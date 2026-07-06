"""
Signed Manifest Tests.

MerkleTree, RFC3161Client, SignedManifest 테스트.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.signed_manifest import (
    MerkleTree,
    RFC3161Client,
    SignedManifest,
    main,
)

# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def temp_dir():
    """임시 디렉토리."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_log_file(temp_dir):
    """샘플 로그 파일 생성."""
    log_file = temp_dir / "audit_2025-01-15.jsonl"

    entries = [
        {
            "audit_id": "audit-001",
            "timestamp": "2025-01-15T10:00:00Z",
            "action": "config_change",
        },
        {
            "audit_id": "audit-002",
            "timestamp": "2025-01-15T11:00:00Z",
            "action": "governance_blocked",
        },
        {
            "audit_id": "audit-003",
            "timestamp": "2025-01-15T12:00:00Z",
            "action": "cb_force_open",
        },
    ]

    with open(log_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    return log_file


@pytest.fixture
def sample_log_dir(temp_dir):
    """여러 로그 파일이 있는 디렉토리."""
    for day in range(1, 4):
        log_file = temp_dir / f"audit_2025-01-{day:02d}.jsonl"
        with open(log_file, "w") as f:
            f.write(
                json.dumps(
                    {
                        "audit_id": f"audit-{day}",
                        "timestamp": f"2025-01-{day:02d}T10:00:00Z",
                    }
                )
                + "\n"
            )

    return temp_dir


# ─────────────────────────────────────────────────────────────
# MerkleTree Tests
# ─────────────────────────────────────────────────────────────


class TestMerkleTree:
    """MerkleTree 테스트."""

    def test_empty_tree(self):
        """빈 트리."""
        tree = MerkleTree()
        root = tree.compute_root()

        # 빈 해시
        assert len(root) == 32  # SHA-256 = 32 bytes

    def test_single_leaf(self):
        """단일 리프."""
        tree = MerkleTree()
        tree.add_leaf(b"hello")

        root = tree.compute_root()

        assert len(root) == 32
        assert tree.leaf_count == 1

    def test_two_leaves(self):
        """두 개의 리프."""
        tree = MerkleTree()
        tree.add_leaf(b"hello")
        tree.add_leaf(b"world")

        root = tree.compute_root()

        assert len(root) == 32
        assert tree.leaf_count == 2

    def test_odd_number_of_leaves(self):
        """홀수 개의 리프."""
        tree = MerkleTree()
        tree.add_leaf(b"a")
        tree.add_leaf(b"b")
        tree.add_leaf(b"c")

        root = tree.compute_root()

        assert len(root) == 32
        assert tree.leaf_count == 3

    def test_deterministic_root(self):
        """같은 입력 = 같은 루트."""
        tree1 = MerkleTree()
        tree1.add_leaf(b"data1")
        tree1.add_leaf(b"data2")

        tree2 = MerkleTree()
        tree2.add_leaf(b"data1")
        tree2.add_leaf(b"data2")

        assert tree1.compute_root() == tree2.compute_root()

    def test_different_input_different_root(self):
        """다른 입력 = 다른 루트."""
        tree1 = MerkleTree()
        tree1.add_leaf(b"data1")
        tree1.add_leaf(b"data2")

        tree2 = MerkleTree()
        tree2.add_leaf(b"data1")
        tree2.add_leaf(b"data3")  # 다름

        assert tree1.compute_root() != tree2.compute_root()

    def test_order_matters(self):
        """순서가 중요함."""
        tree1 = MerkleTree()
        tree1.add_leaf(b"a")
        tree1.add_leaf(b"b")

        tree2 = MerkleTree()
        tree2.add_leaf(b"b")
        tree2.add_leaf(b"a")

        assert tree1.compute_root() != tree2.compute_root()

    def test_compute_root_hex(self):
        """16진수 루트."""
        tree = MerkleTree()
        tree.add_leaf(b"test")

        root_hex = tree.compute_root_hex()

        assert len(root_hex) == 64  # 32 bytes * 2
        assert all(c in "0123456789abcdef" for c in root_hex)

    def test_get_proof(self):
        """Merkle Proof 생성."""
        tree = MerkleTree()
        for i in range(4):
            tree.add_leaf(f"leaf{i}".encode())

        tree.compute_root()
        proof = tree.get_proof(0)  # 첫 번째 리프의 proof

        assert len(proof) == 2  # 4개 리프 = 2레벨

    def test_verify_proof(self):
        """Merkle Proof 검증."""
        tree = MerkleTree()
        data = b"test_data"
        tree.add_leaf(data)
        tree.add_leaf(b"other")

        root = tree.compute_root()
        proof = tree.get_proof(0)

        # 원본 데이터의 해시로 검증
        import hashlib

        leaf_hash = hashlib.sha256(data).digest()

        assert tree.verify_proof(leaf_hash, proof, root)

    def test_verify_proof_fails_for_tampered_data(self):
        """변조된 데이터는 검증 실패."""
        tree = MerkleTree()
        tree.add_leaf(b"original")
        tree.add_leaf(b"other")

        root = tree.compute_root()
        proof = tree.get_proof(0)

        # 변조된 데이터
        import hashlib

        tampered_hash = hashlib.sha256(b"tampered").digest()

        assert not tree.verify_proof(tampered_hash, proof, root)

    def test_large_tree(self):
        """대용량 트리."""
        tree = MerkleTree()
        for i in range(1000):
            tree.add_leaf(f"entry_{i}".encode())

        root = tree.compute_root()

        assert len(root) == 32
        assert tree.leaf_count == 1000


# ─────────────────────────────────────────────────────────────
# RFC3161Client Tests
# ─────────────────────────────────────────────────────────────


class TestRFC3161Client:
    """RFC3161Client 테스트."""

    def test_default_tsa_url(self):
        """기본 TSA URL."""
        client = RFC3161Client()

        assert client._tsa_url == "https://freetsa.org/tsr"

    def test_custom_tsa_url(self):
        """커스텀 TSA URL."""
        client = RFC3161Client(tsa_url="http://custom-tsa.example.com")

        assert client._tsa_url == "http://custom-tsa.example.com"

    def test_get_timestamp_success(self):
        """타임스탬프 발급 성공."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.read.return_value = b"timestamp_token"
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            client = RFC3161Client()
            data_hash = bytes.fromhex("abcd1234" * 8)

            timestamp = client.get_timestamp(data_hash)

            assert timestamp is not None
            assert timestamp.message_imprint == data_hash.hex()

    def test_get_timestamp_failure(self):
        """타임스탬프 발급 실패."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = Exception("Connection refused")

            client = RFC3161Client()
            data_hash = bytes.fromhex("abcd1234" * 8)

            timestamp = client.get_timestamp(data_hash)

            assert timestamp is None


# ─────────────────────────────────────────────────────────────
# SignedManifest Tests
# ─────────────────────────────────────────────────────────────


class TestSignedManifest:
    """SignedManifest 테스트."""

    def test_add_log_file(self, sample_log_file):
        """로그 파일 추가."""
        manifest = SignedManifest(enable_timestamp=False)
        entry = manifest.add_log_file(sample_log_file)

        assert entry.entry_count == 3
        assert entry.first_timestamp == "2025-01-15T10:00:00Z"
        assert entry.last_timestamp == "2025-01-15T12:00:00Z"
        assert len(entry.file_hash) == 64  # SHA-256 hex

    def test_add_log_file_not_found(self, temp_dir):
        """없는 파일 추가."""
        manifest = SignedManifest(enable_timestamp=False)

        with pytest.raises(FileNotFoundError):
            manifest.add_log_file(temp_dir / "nonexistent.jsonl")

    def test_add_log_directory(self, sample_log_dir):
        """디렉토리의 모든 파일 추가."""
        manifest = SignedManifest(enable_timestamp=False)
        entries = manifest.add_log_directory(sample_log_dir)

        assert len(entries) == 3
        assert all(e.entry_count == 1 for e in entries)

    def test_compute_merkle_root(self, sample_log_file):
        """머클 루트 계산."""
        manifest = SignedManifest(enable_timestamp=False)
        manifest.add_log_file(sample_log_file)

        root = manifest.compute_merkle_root()

        assert len(root) == 64  # SHA-256 hex

    def test_merkle_root_deterministic(self, sample_log_file):
        """같은 파일 = 같은 머클 루트."""
        manifest1 = SignedManifest(enable_timestamp=False)
        manifest1.add_log_file(sample_log_file)
        root1 = manifest1.compute_merkle_root()

        manifest2 = SignedManifest(enable_timestamp=False)
        manifest2.add_log_file(sample_log_file)
        root2 = manifest2.compute_merkle_root()

        assert root1 == root2

    def test_merkle_root_changes_on_modification(self, temp_dir):
        """파일 수정 시 루트 변경."""
        log_file = temp_dir / "test.jsonl"

        # 원본 파일
        with open(log_file, "w") as f:
            f.write('{"id": 1}\n')

        manifest1 = SignedManifest(enable_timestamp=False)
        manifest1.add_log_file(log_file)
        root1 = manifest1.compute_merkle_root()

        # 파일 수정
        with open(log_file, "w") as f:
            f.write('{"id": 2}\n')  # 다른 내용

        manifest2 = SignedManifest(enable_timestamp=False)
        manifest2.add_log_file(log_file)
        root2 = manifest2.compute_merkle_root()

        assert root1 != root2

    def test_compute_and_timestamp(self, sample_log_file):
        """머클 루트 + 타임스탬프 한번에."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.read.return_value = b"token"
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            manifest = SignedManifest(enable_timestamp=True)
            manifest.add_log_file(sample_log_file)

            root, timestamp = manifest.compute_and_timestamp()

            assert len(root) == 64
            assert timestamp is not None

    def test_to_dict(self, sample_log_file):
        """딕셔너리 변환."""
        manifest = SignedManifest(enable_timestamp=False)
        manifest.add_log_file(sample_log_file)
        manifest.compute_merkle_root()

        data = manifest.to_dict()

        assert "version" in data
        assert "created_at" in data
        assert "merkle_root" in data
        assert "entries" in data
        assert len(data["entries"]) == 1
        assert data["metadata"]["leaf_count"] == 3

    def test_save_and_load(self, sample_log_file, temp_dir):
        """저장 및 로드."""
        manifest_file = temp_dir / "manifest.json"

        # 생성 및 저장
        manifest1 = SignedManifest(enable_timestamp=False)
        manifest1.add_log_file(sample_log_file)
        manifest1.compute_merkle_root()
        manifest1.save(manifest_file)

        # 로드
        manifest2 = SignedManifest.load(manifest_file)

        assert manifest2._merkle_root == manifest1._merkle_root
        assert len(manifest2._entries) == 1

    def test_verify_success(self, sample_log_file, temp_dir):
        """검증 성공."""
        manifest_file = temp_dir / "manifest.json"

        manifest = SignedManifest(enable_timestamp=False)
        manifest.add_log_file(sample_log_file)
        manifest.compute_merkle_root()
        manifest.save(manifest_file)

        # 로드 후 검증
        loaded = SignedManifest.load(manifest_file)

        assert loaded.verify()

    def test_verify_fails_on_modification(self, sample_log_file, temp_dir):
        """파일 수정 시 검증 실패."""
        manifest_file = temp_dir / "manifest.json"

        manifest = SignedManifest(enable_timestamp=False)
        manifest.add_log_file(sample_log_file)
        manifest.compute_merkle_root()
        manifest.save(manifest_file)

        # 로그 파일 수정
        with open(sample_log_file, "a") as f:
            f.write('{"id": "tampered"}\n')

        # 로드 후 검증
        loaded = SignedManifest.load(manifest_file)

        assert not loaded.verify()

    def test_verify_fails_on_missing_file(self, sample_log_file, temp_dir):
        """파일 삭제 시 검증 실패."""
        manifest_file = temp_dir / "manifest.json"

        manifest = SignedManifest(enable_timestamp=False)
        manifest.add_log_file(sample_log_file)
        manifest.compute_merkle_root()
        manifest.save(manifest_file)

        # 로그 파일 삭제
        sample_log_file.unlink()

        # 로드 후 검증
        loaded = SignedManifest.load(manifest_file)

        assert not loaded.verify()


# ─────────────────────────────────────────────────────────────
# CLI Tests
# ─────────────────────────────────────────────────────────────


class TestCLI:
    """CLI 테스트."""

    def test_create_manifest(self, sample_log_file, temp_dir, capsys):
        """매니페스트 생성."""
        manifest_file = temp_dir / "manifest.json"

        with patch(
            "sys.argv",
            [
                "signed_manifest",
                "create",
                "--input",
                str(sample_log_file),
                "--output",
                str(manifest_file),
                "--no-timestamp",
            ],
        ):
            main()

        assert manifest_file.exists()

        captured = capsys.readouterr()
        assert "Manifest created" in captured.out

    def test_verify_manifest_success(self, sample_log_file, temp_dir, capsys):
        """매니페스트 검증 성공."""
        manifest_file = temp_dir / "manifest.json"

        # 먼저 생성
        manifest = SignedManifest(enable_timestamp=False)
        manifest.add_log_file(sample_log_file)
        manifest.compute_merkle_root()
        manifest.save(manifest_file)

        # 검증
        with patch(
            "sys.argv",
            [
                "signed_manifest",
                "verify",
                str(manifest_file),
            ],
        ):
            # Expect SystemExit with code 0
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Verification passed" in captured.out


# ─────────────────────────────────────────────────────────────
# Edge Cases
# ─────────────────────────────────────────────────────────────


class TestEdgeCases:
    """엣지 케이스 테스트."""

    def test_empty_log_file(self, temp_dir):
        """빈 로그 파일."""
        empty_file = temp_dir / "empty.jsonl"
        empty_file.touch()

        manifest = SignedManifest(enable_timestamp=False)
        entry = manifest.add_log_file(empty_file)

        assert entry.entry_count == 0
        assert entry.first_timestamp is None

    def test_log_file_with_blank_lines(self, temp_dir):
        """빈 줄이 있는 로그 파일."""
        log_file = temp_dir / "with_blanks.jsonl"
        with open(log_file, "w") as f:
            f.write('{"id": 1}\n')
            f.write("\n")  # 빈 줄
            f.write('{"id": 2}\n')
            f.write("   \n")  # 공백만 있는 줄
            f.write('{"id": 3}\n')

        manifest = SignedManifest(enable_timestamp=False)
        entry = manifest.add_log_file(log_file)

        assert entry.entry_count == 3

    def test_unicode_content(self, temp_dir):
        """유니코드 내용."""
        log_file = temp_dir / "unicode.jsonl"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write('{"message": "한글 테스트"}\n')
            f.write('{"message": "日本語テスト"}\n')

        manifest = SignedManifest(enable_timestamp=False)
        entry = manifest.add_log_file(log_file)

        assert entry.entry_count == 2

    def test_large_manifest(self, temp_dir):
        """대용량 매니페스트."""
        log_file = temp_dir / "large.jsonl"

        with open(log_file, "w") as f:
            for i in range(10000):
                f.write(json.dumps({"id": i}) + "\n")

        manifest = SignedManifest(enable_timestamp=False)
        entry = manifest.add_log_file(log_file)
        root = manifest.compute_merkle_root()

        assert entry.entry_count == 10000
        assert len(root) == 64
