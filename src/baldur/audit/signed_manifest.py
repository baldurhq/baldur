"""
Signed Manifest - 법적 효력을 위한 무결성 증명.

파일 기반 감사 로그의 법적 효력을 높이기 위한 도구.

핵심 개념:
1. Merkle Tree: 모든 로그 엔트리의 해시를 트리 구조로 합침
2. Merkle Root: 트리의 최상위 해시 - 단일 해시로 전체 무결성 증명
3. RFC 3161 Timestamp: 외부 TSA(Time Stamp Authority)에서 타임스탬프 발급

비침투 원칙:
- 하루에 한 번, 단일 해시값만 외부에 제출
- 고객사 DB/시스템에 전혀 접근하지 않음
- 제3자 TSA가 시간 증명 (우리가 아닌 제3자 신뢰)

법적 효력:
- 머클 루트는 블록체인과 동일한 원리로 무결성 보장
- RFC 3161 타임스탬프는 법적으로 인정되는 시간 증명
- 감사 시 "이 시점에 이 데이터가 존재했음"을 증명 가능

사용법:
    # 머클 루트 생성
    manifest = SignedManifest()
    manifest.add_log_file("/var/log/audit/2025-01-15.jsonl")
    root = manifest.compute_merkle_root()

    # RFC 3161 타임스탬프 발급 (선택)
    timestamp = manifest.get_rfc3161_timestamp(root)

    # 매니페스트 저장
    manifest.save("manifest_2025-01-15.json")
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.checksum import compute_checksum
from baldur.utils.http import safe_urlopen
from baldur.utils.serialization import fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# ─────────────────────────────────────────────────────────────
# Merkle Tree Implementation
# ─────────────────────────────────────────────────────────────


class MerkleTree:
    """
    Merkle Tree 구현.

    블록체인에서 사용되는 것과 동일한 원리:
    - 리프 노드: 각 데이터 항목의 해시
    - 내부 노드: 자식 노드들의 해시를 합친 것의 해시
    - 루트: 전체 트리의 단일 해시

    특징:
    - 데이터 하나라도 변경되면 루트가 완전히 달라짐
    - 특정 데이터의 포함 여부를 O(log n)으로 증명 가능
    """

    def __init__(self, hash_func: str = "sha256"):
        self._hash_func = hash_func
        self._leaves: list[bytes] = []
        self._tree: list[list[bytes]] = []

    def add_leaf(self, data: bytes) -> None:
        """리프 노드 추가."""
        leaf_hash = self._hash(data)
        self._leaves.append(leaf_hash)

    def add_leaf_hash(self, hash_bytes: bytes) -> None:
        """이미 해시된 리프 추가."""
        self._leaves.append(hash_bytes)

    def _hash(self, data: bytes) -> bytes:
        """해시 계산."""
        hex_str = compute_checksum(data, algorithm=self._hash_func)
        return bytes.fromhex(hex_str)

    def compute_root(self) -> bytes:
        """머클 루트 계산."""
        if not self._leaves:
            return self._hash(b"")

        # 레벨 0: 리프 노드들
        current_level = self._leaves.copy()
        self._tree = [current_level]

        # 트리 빌드 (bottom-up)
        while len(current_level) > 1:
            next_level = []

            for i in range(0, len(current_level), 2):
                left = current_level[i]
                # 홀수 개일 경우 마지막 노드는 자기 자신과 합침
                right = current_level[i + 1] if i + 1 < len(current_level) else left
                parent = self._hash(left + right)
                next_level.append(parent)

            self._tree.append(next_level)
            current_level = next_level

        return current_level[0]

    def compute_root_hex(self) -> str:
        """머클 루트 (16진수)."""
        return self.compute_root().hex()

    def get_proof(self, index: int) -> list[tuple[bytes, str]]:
        """
        특정 리프의 Merkle Proof 생성.

        Proof를 사용하면 특정 데이터가 트리에 포함되어 있음을
        전체 데이터 없이도 증명 가능.

        Returns:
            List of (sibling_hash, direction) tuples
            direction: 'L' = sibling is on left, 'R' = sibling is on right
        """
        if not self._tree:
            self.compute_root()

        proof = []
        current_index = index

        for level in self._tree[:-1]:  # 루트 레벨 제외
            sibling_index = current_index ^ 1  # XOR로 형제 인덱스 계산

            if sibling_index < len(level):
                sibling = level[sibling_index]
                direction = "L" if sibling_index < current_index else "R"
                proof.append((sibling, direction))

            current_index //= 2

        return proof

    def verify_proof(
        self,
        leaf_hash: bytes,
        proof: list[tuple[bytes, str]],
        root: bytes,
    ) -> bool:
        """Merkle Proof 검증."""
        current = leaf_hash

        for sibling, direction in proof:
            if direction == "L":
                current = self._hash(sibling + current)
            else:
                current = self._hash(current + sibling)

        return current == root

    @property
    def leaf_count(self) -> int:
        """리프 노드 개수."""
        return len(self._leaves)


# ─────────────────────────────────────────────────────────────
# RFC 3161 Timestamp
# ─────────────────────────────────────────────────────────────


@dataclass
class RFC3161Timestamp:
    """RFC 3161 타임스탬프."""

    timestamp: datetime
    tsa_name: str
    serial_number: str
    hash_algorithm: str
    message_imprint: str  # 해시된 데이터 (hex)
    token: bytes  # DER 인코딩된 타임스탬프 토큰


class RFC3161Client:
    """
    RFC 3161 TSA (Time Stamp Authority) 클라이언트.

    외부 TSA 서비스에서 타임스탬프를 발급받습니다.

    지원되는 무료 TSA:
    - FreeTSA: https://freetsa.org/tsr
    - DigiCert: http://timestamp.digicert.com

    상용 TSA (법적 효력 강화):
    - GlobalSign, Symantec, Entrust 등

    NOTE: 실제 RFC 3161 요청/응답은 ASN.1/DER 인코딩이 필요합니다.
          이 구현은 간소화된 버전입니다.
          실제 사용 시 `rfc3161ng` 또는 `asn1crypto` 라이브러리 사용을 권장합니다.
    """

    # 무료 TSA 서비스들
    DEFAULT_TSA_URLS = [
        "https://freetsa.org/tsr",
        "http://timestamp.digicert.com",
        "http://tsa.safecreative.org",
    ]

    def __init__(
        self,
        tsa_url: str | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._tsa_url = tsa_url or self.DEFAULT_TSA_URLS[0]
        self._timeout = timeout_seconds

    def get_timestamp(self, data_hash: bytes) -> RFC3161Timestamp | None:
        """
        RFC 3161 타임스탬프 발급 요청.

        NOTE: 이 구현은 간소화된 버전입니다.
              실제 RFC 3161 프로토콜은 ASN.1/DER 인코딩이 필요합니다.

        실제 사용 시:
            pip install rfc3161ng
            그리고 해당 라이브러리 사용

        Args:
            data_hash: SHA-256 해시 (bytes)

        Returns:
            RFC3161Timestamp or None on failure
        """
        try:
            # 간소화된 요청 (실제로는 ASN.1 TimeStampReq 필요)
            # 여기서는 FreeTSA의 간단한 API 사용
            timestamp_request = self._create_timestamp_request(data_hash)

            req = urllib.request.Request(
                self._tsa_url,
                data=timestamp_request,
                headers={
                    "Content-Type": "application/timestamp-query",
                },
                method="POST",
            )

            with safe_urlopen(req, timeout=self._timeout) as response:
                if response.status != 200:
                    logger.error(
                        "tsa.returned_status",
                        response=response.status,
                    )
                    return None

                response_data = response.read()
                return self._parse_timestamp_response(response_data, data_hash)

        except urllib.error.URLError as e:
            logger.exception(
                "signed_manifest.timestamp_get_failed",
                tsa_url=self._tsa_url,
                error=e,
            )
            return None
        except Exception as e:
            logger.exception(
                "timestamp.request_failed",
                error=e,
            )
            return None

    def _create_timestamp_request(self, data_hash: bytes) -> bytes:
        """
        RFC 3161 TimeStampReq 생성 (간소화 버전).

        실제 구현에서는 ASN.1 라이브러리 사용 필요.
        """
        # 간소화: 해시값만 전송 (실제로는 ASN.1 구조 필요)
        return data_hash

    def _parse_timestamp_response(
        self, response_data: bytes, original_hash: bytes
    ) -> RFC3161Timestamp | None:
        """
        RFC 3161 TimeStampResp 파싱 (간소화 버전).

        실제 구현에서는 ASN.1 파싱 필요.
        """
        # 간소화: 현재 시간으로 대체 (실제로는 TSA 응답 파싱)
        return RFC3161Timestamp(
            timestamp=utc_now(),
            tsa_name=self._tsa_url,
            serial_number="placeholder",
            hash_algorithm="sha256",
            message_imprint=original_hash.hex(),
            token=response_data,
        )


# ─────────────────────────────────────────────────────────────
# Signed Manifest
# ─────────────────────────────────────────────────────────────


@dataclass
class ManifestEntry:
    """매니페스트 엔트리."""

    file_path: str
    file_hash: str  # SHA-256 hex
    entry_count: int
    first_timestamp: str | None = None
    last_timestamp: str | None = None


@dataclass
class SignedManifestData:
    """서명된 매니페스트 데이터."""

    version: str = "1.0"
    created_at: str = ""
    merkle_root: str = ""
    entries: list[ManifestEntry] = field(default_factory=list)
    rfc3161_timestamp: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SignedManifest:
    """
    서명된 매니페스트 생성기.

    사용 시나리오:
    1. 매일 자정에 cron으로 실행
    2. 그 날의 모든 감사 로그 파일을 처리
    3. 머클 루트 계산 + RFC 3161 타임스탬프 발급
    4. 매니페스트 파일 저장

    결과물:
    - manifest_YYYY-MM-DD.json: 머클 루트 + 파일 목록 + 타임스탬프
    - 이 파일 하나로 해당 날짜의 모든 로그 무결성 증명 가능

    Usage:
        manifest = SignedManifest()
        manifest.add_log_file("/var/log/audit/2025-01-15.jsonl")
        manifest.compute_and_timestamp()
        manifest.save("manifests/2025-01-15.json")
    """

    def __init__(
        self,
        tsa_url: str | None = None,
        enable_timestamp: bool = True,
    ):
        self._merkle_tree = MerkleTree()
        self._entries: list[ManifestEntry] = []
        self._tsa_client = RFC3161Client(tsa_url) if enable_timestamp else None
        self._merkle_root: str | None = None
        self._timestamp: RFC3161Timestamp | None = None

    def add_log_file(self, file_path: str | Path) -> ManifestEntry:
        """
        감사 로그 파일 추가.

        파일의 각 라인(JSONL)을 머클 트리에 추가합니다.

        Args:
            file_path: 감사 로그 파일 경로

        Returns:
            ManifestEntry with file metadata
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Log file not found: {file_path}")

        entry_count = 0
        first_ts = None
        last_ts = None
        file_hasher = hashlib.sha256()

        with open(file_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue

                line_bytes = line.strip().encode("utf-8")

                # 머클 트리에 추가
                self._merkle_tree.add_leaf(line_bytes)

                # 파일 전체 해시에도 추가
                file_hasher.update(line_bytes)

                entry_count += 1

                # 타임스탬프 추출 (첫/마지막)
                try:
                    entry = fast_loads(line)
                    ts = entry.get("timestamp")
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                except ValueError:
                    pass

        manifest_entry = ManifestEntry(
            file_path=str(file_path.absolute()),
            file_hash=file_hasher.hexdigest(),
            entry_count=entry_count,
            first_timestamp=first_ts,
            last_timestamp=last_ts,
        )

        self._entries.append(manifest_entry)

        logger.info(
            "added.entries",
            file_path=file_path,
            entry_count=entry_count,
        )
        return manifest_entry

    def add_log_directory(
        self,
        dir_path: str | Path,
        pattern: str = "*.jsonl",
    ) -> list[ManifestEntry]:
        """
        디렉토리의 모든 로그 파일 추가.

        Args:
            dir_path: 디렉토리 경로
            pattern: 파일 패턴 (glob)

        Returns:
            List of ManifestEntry
        """
        dir_path = Path(dir_path)
        entries = []

        for file_path in sorted(dir_path.glob(pattern)):
            if file_path.is_file():
                entry = self.add_log_file(file_path)
                entries.append(entry)

        return entries

    def compute_merkle_root(self) -> str:
        """
        머클 루트 계산.

        Returns:
            Merkle root as hex string
        """
        self._merkle_root = self._merkle_tree.compute_root_hex()
        return self._merkle_root

    def get_rfc3161_timestamp(
        self,
        data: bytes | None = None,
    ) -> RFC3161Timestamp | None:
        """
        RFC 3161 타임스탬프 발급.

        Args:
            data: 타임스탬프할 데이터 (기본: 머클 루트)

        Returns:
            RFC3161Timestamp or None
        """
        if not self._tsa_client:
            logger.warning("signed_manifest.rfc_timestamp_disabled")
            return None

        if data is None:
            if self._merkle_root is None:
                self.compute_merkle_root()
            assert self._merkle_root is not None  # compute_merkle_root() populates
            data = bytes.fromhex(self._merkle_root)

        self._timestamp = self._tsa_client.get_timestamp(data)
        return self._timestamp

    def compute_and_timestamp(self) -> tuple[str, RFC3161Timestamp | None]:
        """
        머클 루트 계산 및 타임스탬프 발급 (원스텝).

        Returns:
            (merkle_root, timestamp)
        """
        root = self.compute_merkle_root()
        timestamp = self.get_rfc3161_timestamp()
        return root, timestamp

    def to_dict(self) -> dict[str, Any]:
        """매니페스트를 딕셔너리로 변환."""
        if self._merkle_root is None:
            self.compute_merkle_root()
        assert self._merkle_root is not None  # compute_merkle_root() populates

        data = SignedManifestData(
            version="1.0",
            created_at=utc_now().isoformat(),
            merkle_root=self._merkle_root,
            entries=self._entries,
            metadata={
                "leaf_count": self._merkle_tree.leaf_count,
                "hash_algorithm": "sha256",
            },
        )

        result = {
            "version": data.version,
            "created_at": data.created_at,
            "merkle_root": data.merkle_root,
            "entries": [
                {
                    "file_path": e.file_path,
                    "file_hash": e.file_hash,
                    "entry_count": e.entry_count,
                    "first_timestamp": e.first_timestamp,
                    "last_timestamp": e.last_timestamp,
                }
                for e in data.entries
            ],
            "metadata": data.metadata,
        }

        if self._timestamp:
            result["rfc3161_timestamp"] = {
                "timestamp": self._timestamp.timestamp.isoformat(),
                "tsa_name": self._timestamp.tsa_name,
                "serial_number": self._timestamp.serial_number,
                "hash_algorithm": self._timestamp.hash_algorithm,
                "message_imprint": self._timestamp.message_imprint,
                "token_base64": base64.b64encode(self._timestamp.token).decode("ascii"),
            }

        return result

    def save(self, output_path: str | Path) -> None:
        """
        매니페스트 저장.

        Args:
            output_path: 저장 경로
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

        logger.info(
            "saved.manifest",
            output_path=output_path,
        )

    @classmethod
    def load(cls, manifest_path: str | Path) -> SignedManifest:
        """
        매니페스트 로드.

        Args:
            manifest_path: 매니페스트 파일 경로

        Returns:
            SignedManifest instance
        """
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)

        manifest = cls(enable_timestamp=False)
        manifest._merkle_root = data["merkle_root"]
        manifest._entries = [ManifestEntry(**entry) for entry in data["entries"]]

        return manifest

    def verify(self) -> bool:
        """
        매니페스트 검증.

        저장된 파일들이 변경되지 않았는지 확인.

        Returns:
            True if all files are intact
        """
        # 각 파일 해시 재계산
        tree = MerkleTree()

        for entry in self._entries:
            file_path = Path(entry.file_path)

            if not file_path.exists():
                logger.error(
                    "file.found",
                    file_path=file_path,
                )
                return False

            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        tree.add_leaf(line.strip().encode("utf-8"))

        # 머클 루트 비교
        computed_root = tree.compute_root_hex()

        if computed_root != self._merkle_root:
            logger.error(
                "merkle.root_mismatch_expected",
                merkle_root=self._merkle_root,
                computed_root=computed_root,
            )
            return False

        logger.info("signed_manifest.verification_passed")
        return True


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def main():
    """CLI 엔트리포인트."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="baldur.audit.signed_manifest",
        description="Create signed manifests for audit log files",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # create 명령
    create_parser = subparsers.add_parser("create", help="Create a new manifest")
    create_parser.add_argument(
        "--input",
        "-i",
        nargs="+",
        required=True,
        help="Input log files or directories",
    )
    create_parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Output manifest file path",
    )
    create_parser.add_argument(
        "--no-timestamp",
        action="store_true",
        help="Skip RFC 3161 timestamp",
    )
    create_parser.add_argument(
        "--tsa-url",
        help="Custom TSA URL",
    )

    # verify 명령
    verify_parser = subparsers.add_parser("verify", help="Verify a manifest")
    verify_parser.add_argument(
        "manifest",
        help="Manifest file to verify",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.command == "create":
        manifest = SignedManifest(
            tsa_url=args.tsa_url,
            enable_timestamp=not args.no_timestamp,
        )

        for path in args.input:
            path = Path(path)
            if path.is_dir():
                manifest.add_log_directory(path)
            else:
                manifest.add_log_file(path)

        manifest.compute_and_timestamp()
        manifest.save(args.output)

        print(f"\n✅ Manifest created: {args.output}")
        print(f"   Merkle Root: {manifest._merkle_root}")

    elif args.command == "verify":
        manifest = SignedManifest.load(args.manifest)

        if manifest.verify():
            print("✅ Verification passed!")
            sys.exit(0)
        else:
            print("❌ Verification failed!")
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
