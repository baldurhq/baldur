#!/usr/bin/env python3
"""
Storage 성능 벤치마크.

LMDB DiskPersistentBuffer 사용을 위한 스토리지 성능을 측정합니다.

사용법:
    # 로컬에서 실행
    python scripts/benchmark_storage.py /var/lib/baldur/buffer

    # Kubernetes Pod에서 실행
    kubectl exec -it baldur-worker-xxx -- python scripts/benchmark_storage.py

목표 성능:
    - 4K Random Write IOPS: >= 1,000 (목표 TPS 달성용)
    - fsync Latency: <= 10ms (Group Commit 간격)
    - Sequential Write Throughput: >= 50 MB/s
"""

from __future__ import annotations

import os
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StorageBenchmarkResult:
    """벤치마크 결과."""

    test_path: str
    random_write_iops: float = 0.0
    fsync_latency_ms: float = 0.0
    fsync_p99_latency_ms: float = 0.0
    sequential_write_mbps: float = 0.0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    filesystem_type: str = ""
    passed: bool = False
    recommendations: list[str] = field(default_factory=list)


def get_filesystem_type(path: str) -> str:
    """파일 시스템 유형 확인."""
    try:
        if sys.platform == "win32":
            import ctypes

            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(os.path.splitdrive(path)[0] + "\\"),
                None,
                0,
                None,
                None,
                None,
                buf,
                256,
            )
            return buf.value
        else:
            import subprocess

            result = subprocess.run(
                ["df", "-T", path],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                if len(lines) > 1:
                    return lines[1].split()[1]
    except Exception:
        pass
    return "unknown"


def benchmark_fsync_latency(
    test_path: str,
    iterations: int = 100,
) -> tuple[float, float, list[float]]:
    """
    fsync 지연 시간 측정.

    Returns:
        (평균 ms, P99 ms, 전체 지연 목록)
    """
    latencies = []
    test_file = os.path.join(test_path, "fsync_test.tmp")

    try:
        for _ in range(iterations):
            with open(test_file, "wb") as f:
                f.write(os.urandom(4096))  # 4KB
                f.flush()
                start = time.perf_counter()
                os.fsync(f.fileno())
                latency_ms = (time.perf_counter() - start) * 1000
                latencies.append(latency_ms)

        avg = statistics.mean(latencies)
        p99 = sorted(latencies)[int(len(latencies) * 0.99)]
        return avg, p99, latencies

    finally:
        if os.path.exists(test_file):
            os.remove(test_file)


def benchmark_random_write_iops(
    test_path: str,
    duration_seconds: float = 5.0,
) -> float:
    """
    Random Write IOPS 측정.

    Returns:
        IOPS
    """
    test_file = os.path.join(test_path, "iops_test.tmp")

    try:
        start = time.perf_counter()
        ops = 0

        while (time.perf_counter() - start) < duration_seconds:
            with open(test_file, "wb") as f:
                f.write(os.urandom(4096))  # 4KB
                f.flush()
                os.fsync(f.fileno())
            ops += 1

        elapsed = time.perf_counter() - start
        return ops / elapsed

    finally:
        if os.path.exists(test_file):
            os.remove(test_file)


def benchmark_sequential_write(
    test_path: str,
    chunk_size_mb: int = 1,
    total_mb: int = 100,
) -> float:
    """
    Sequential Write Throughput 측정.

    Returns:
        MB/s
    """
    test_file = os.path.join(test_path, "seq_test.tmp")
    chunk_bytes = chunk_size_mb * 1024 * 1024
    chunks = total_mb // chunk_size_mb

    try:
        start = time.perf_counter()

        with open(test_file, "wb") as f:
            for _ in range(chunks):
                f.write(os.urandom(chunk_bytes))
            f.flush()
            os.fsync(f.fileno())

        elapsed = time.perf_counter() - start
        return total_mb / elapsed

    finally:
        if os.path.exists(test_file):
            os.remove(test_file)


def benchmark_storage(
    test_path: str = "/var/lib/baldur/buffer",
    target_iops: int = 1000,
    target_latency_ms: float = 10.0,
) -> StorageBenchmarkResult:
    """
    스토리지 성능 벤치마크 실행.

    Args:
        test_path: 테스트 경로
        target_iops: 목표 IOPS
        target_latency_ms: 목표 fsync 지연 시간

    Returns:
        StorageBenchmarkResult
    """
    result = StorageBenchmarkResult(test_path=test_path)

    # 디렉토리 생성
    Path(test_path).mkdir(parents=True, exist_ok=True)

    print(f"\n📊 Storage Benchmark: {test_path}")
    print("=" * 60)

    # 디스크 정보
    try:
        usage = shutil.disk_usage(test_path)
        result.disk_free_gb = usage.free / (1024**3)
        result.disk_total_gb = usage.total / (1024**3)
        print(f"💾 Disk: {result.disk_free_gb:.1f} GB free / " f"{result.disk_total_gb:.1f} GB total")
    except Exception as e:
        result.recommendations.append(f"디스크 정보 조회 실패: {e}")

    # 파일 시스템 확인
    result.filesystem_type = get_filesystem_type(test_path)
    print(f"📁 Filesystem: {result.filesystem_type}")

    print("-" * 60)

    # 1. fsync Latency 측정
    print("⏱️  Testing fsync latency (100 iterations)...")
    try:
        avg, p99, _ = benchmark_fsync_latency(test_path)
        result.fsync_latency_ms = avg
        result.fsync_p99_latency_ms = p99
        status = "✅" if avg <= target_latency_ms else "❌"
        print(f"   Average: {avg:.2f} ms {status}")
        print(f"   P99:     {p99:.2f} ms")
    except Exception as e:
        result.recommendations.append(f"fsync 테스트 실패: {e}")
        print(f"   ❌ Failed: {e}")

    # 2. Random Write IOPS 측정
    print("\n🔧 Testing random write IOPS (5 seconds)...")
    try:
        iops = benchmark_random_write_iops(test_path)
        result.random_write_iops = iops
        status = "✅" if iops >= target_iops else "❌"
        print(f"   IOPS: {iops:.0f} {status}")
    except Exception as e:
        result.recommendations.append(f"IOPS 테스트 실패: {e}")
        print(f"   ❌ Failed: {e}")

    # 3. Sequential Write Throughput
    print("\n📝 Testing sequential write throughput (100 MB)...")
    try:
        mbps = benchmark_sequential_write(test_path)
        result.sequential_write_mbps = mbps
        status = "✅" if mbps >= 50 else "⚠️"
        print(f"   Throughput: {mbps:.1f} MB/s {status}")
    except Exception as e:
        result.recommendations.append(f"Throughput 테스트 실패: {e}")
        print(f"   ❌ Failed: {e}")

    # 결과 평가
    result.passed = result.random_write_iops >= target_iops and result.fsync_latency_ms <= target_latency_ms

    # 권장사항 생성
    if not result.passed:
        if result.random_write_iops < target_iops:
            result.recommendations.append(
                f"IOPS 부족: {result.random_write_iops:.0f} < {target_iops}. "
                "SSD StorageClass 사용 또는 Group Commit 배치 크기 증가 권장."
            )
        if result.fsync_latency_ms > target_latency_ms:
            result.recommendations.append(
                f"fsync 지연: {result.fsync_latency_ms:.1f}ms > {target_latency_ms}ms. "
                "sync_on_write=False + 주기적 sync 권장."
            )

    # 파일 시스템별 권장사항
    fs_recommendations = {
        "ext4": "ext4: LMDB writemap=True 권장 (성능 향상)",
        "xfs": "XFS: writemap=True + 큰 allocation group 권장",
        "nfs": "⚠️ NFS: writemap=False 필수 (지원 안함), 성능 저하 예상",
        "tmpfs": "tmpfs: 휘발성! 테스트용으로만 사용",
        "NTFS": "NTFS: Windows 환경, writemap=True 권장",
    }

    fs_lower = result.filesystem_type.lower()
    for fs, rec in fs_recommendations.items():
        if fs.lower() in fs_lower:
            result.recommendations.append(rec)
            break

    return result


def print_benchmark_report(result: StorageBenchmarkResult) -> None:
    """벤치마크 결과 출력."""
    print("\n" + "=" * 60)
    print("📋 BENCHMARK REPORT")
    print("=" * 60)
    print(f"Test Path:        {result.test_path}")
    print(f"Filesystem:       {result.filesystem_type}")
    print(f"Disk Free:        {result.disk_free_gb:.1f} GB")
    print("-" * 60)
    print(f"Random Write IOPS:   {result.random_write_iops:.0f}")
    print(f"fsync Latency:       {result.fsync_latency_ms:.2f} ms (avg)")
    print(f"fsync Latency P99:   {result.fsync_p99_latency_ms:.2f} ms")
    print(f"Sequential Write:    {result.sequential_write_mbps:.1f} MB/s")
    print("-" * 60)

    if result.passed:
        print("Result:           ✅ PASSED")
    else:
        print("Result:           ❌ FAILED")

    if result.recommendations:
        print("\n💡 Recommendations:")
        for rec in result.recommendations:
            print(f"   • {rec}")

    print()


def main() -> int:
    """메인 함수."""
    # 경로 결정
    if len(sys.argv) > 1:
        test_path = sys.argv[1]
    else:
        # 기본 경로
        if sys.platform == "win32":
            test_path = os.path.join(tempfile.gettempdir(), "baldur", "benchmark")
        else:
            test_path = "/var/lib/baldur/buffer"

    # 벤치마크 실행
    result = benchmark_storage(test_path)
    print_benchmark_report(result)

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
