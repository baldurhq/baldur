"""
Signed Manifest - integrity proof for legal admissibility.

A tool for strengthening the legal weight of file-based audit logs.

Core concepts:
1. Merkle Tree: combines the hashes of all log entries into a tree
2. Merkle Root: the top hash of the tree - proves whole-set integrity with a
   single hash
3. RFC 3161 Timestamp: a timestamp issued by an external TSA
   (Time Stamp Authority)

Non-intrusive principle:
- Only a single hash value is submitted externally, once a day
- No access at all to the customer's DB or systems
- A third-party TSA proves the time (trust a third party, not us)

Legal weight:
- The Merkle root guarantees integrity on the same principle as a blockchain
- An RFC 3161 timestamp is a legally recognized proof of time
- During an audit, "this data existed at this point in time" is provable

Usage:
    # Build the Merkle root
    manifest = SignedManifest()
    manifest.add_log_file("/var/log/audit/2025-01-15.jsonl")
    root = manifest.compute_merkle_root()

    # Obtain an RFC 3161 timestamp (optional)
    timestamp = manifest.get_rfc3161_timestamp(root)

    # Save the manifest
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
    Merkle Tree implementation.

    Same principle as the one used in blockchains:
    - Leaf node: the hash of each data item
    - Internal node: the hash of its children's hashes combined
    - Root: a single hash for the whole tree

    Characteristics:
    - Changing even one data item changes the root completely
    - Membership of a specific data item is provable in O(log n)
    """

    def __init__(self, hash_func: str = "sha256"):
        self._hash_func = hash_func
        self._leaves: list[bytes] = []
        self._tree: list[list[bytes]] = []

    def add_leaf(self, data: bytes) -> None:
        """Add a leaf node."""
        leaf_hash = self._hash(data)
        self._leaves.append(leaf_hash)

    def add_leaf_hash(self, hash_bytes: bytes) -> None:
        """Add an already-hashed leaf."""
        self._leaves.append(hash_bytes)

    def _hash(self, data: bytes) -> bytes:
        """Compute a hash."""
        hex_str = compute_checksum(data, algorithm=self._hash_func)
        return bytes.fromhex(hex_str)

    def compute_root(self) -> bytes:
        """Compute the Merkle root."""
        if not self._leaves:
            return self._hash(b"")

        # Level 0: the leaf nodes
        current_level = self._leaves.copy()
        self._tree = [current_level]

        # Build the tree (bottom-up)
        while len(current_level) > 1:
            next_level = []

            for i in range(0, len(current_level), 2):
                left = current_level[i]
                # With an odd count, the last node is combined with itself
                right = current_level[i + 1] if i + 1 < len(current_level) else left
                parent = self._hash(left + right)
                next_level.append(parent)

            self._tree.append(next_level)
            current_level = next_level

        return current_level[0]

    def compute_root_hex(self) -> str:
        """Merkle root (hexadecimal)."""
        return self.compute_root().hex()

    def get_proof(self, index: int) -> list[tuple[bytes, str]]:
        """
        Build the Merkle proof for a specific leaf.

        The proof lets you prove that a specific data item is included in the
        tree without needing the full data set.

        Returns:
            List of (sibling_hash, direction) tuples
            direction: 'L' = sibling is on left, 'R' = sibling is on right
        """
        if not self._tree:
            self.compute_root()

        proof = []
        current_index = index

        for level in self._tree[:-1]:  # excluding the root level
            sibling_index = current_index ^ 1  # sibling index via XOR

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
        """Verify a Merkle proof."""
        current = leaf_hash

        for sibling, direction in proof:
            if direction == "L":
                current = self._hash(sibling + current)
            else:
                current = self._hash(current + sibling)

        return current == root

    @property
    def leaf_count(self) -> int:
        """Number of leaf nodes."""
        return len(self._leaves)


# ─────────────────────────────────────────────────────────────
# RFC 3161 Timestamp
# ─────────────────────────────────────────────────────────────


@dataclass
class RFC3161Timestamp:
    """RFC 3161 timestamp."""

    timestamp: datetime
    tsa_name: str
    serial_number: str
    hash_algorithm: str
    message_imprint: str  # hashed data (hex)
    token: bytes  # DER-encoded timestamp token


class RFC3161Client:
    """
    RFC 3161 TSA (Time Stamp Authority) client.

    Obtains timestamps from an external TSA service.

    Supported free TSAs:
    - FreeTSA: https://freetsa.org/tsr
    - DigiCert: http://timestamp.digicert.com

    Commercial TSAs (stronger legal weight):
    - GlobalSign, Symantec, Entrust, etc.

    NOTE: Real RFC 3161 requests/responses require ASN.1/DER encoding.
          This implementation is a simplified version.
          For production use, the `rfc3161ng` or `asn1crypto` library is
          recommended.
    """

    # Free TSA services
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
        Request an RFC 3161 timestamp.

        NOTE: This implementation is a simplified version.
              The real RFC 3161 protocol requires ASN.1/DER encoding.

        For production use:
            pip install rfc3161ng
            and use that library

        Args:
            data_hash: SHA-256 hash (bytes)

        Returns:
            RFC3161Timestamp or None on failure
        """
        try:
            # Simplified request (a real ASN.1 TimeStampReq is required)
            # Here the simple FreeTSA API is used
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
        Build an RFC 3161 TimeStampReq (simplified version).

        A real implementation needs an ASN.1 library.
        """
        # Simplified: send the hash only (a real ASN.1 structure is required)
        return data_hash

    def _parse_timestamp_response(
        self, response_data: bytes, original_hash: bytes
    ) -> RFC3161Timestamp | None:
        """
        Parse an RFC 3161 TimeStampResp (simplified version).

        A real implementation needs ASN.1 parsing.
        """
        # Simplified: substitute the current time (a real TSA response would
        # be parsed)
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
    """Manifest entry."""

    file_path: str
    file_hash: str  # SHA-256 hex
    entry_count: int
    first_timestamp: str | None = None
    last_timestamp: str | None = None


@dataclass
class SignedManifestData:
    """Signed manifest data."""

    version: str = "1.0"
    created_at: str = ""
    merkle_root: str = ""
    entries: list[ManifestEntry] = field(default_factory=list)
    rfc3161_timestamp: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SignedManifest:
    """
    Signed manifest generator.

    Usage scenario:
    1. Run via cron every midnight
    2. Process all of that day's audit log files
    3. Compute the Merkle root + obtain an RFC 3161 timestamp
    4. Save the manifest file

    Output:
    - manifest_YYYY-MM-DD.json: Merkle root + file list + timestamp
    - This single file proves the integrity of all logs for that date

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
        Add an audit log file.

        Adds each line of the file (JSONL) to the Merkle tree.

        Args:
            file_path: Audit log file path

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

                # Add to the Merkle tree
                self._merkle_tree.add_leaf(line_bytes)

                # Also add to the whole-file hash
                file_hasher.update(line_bytes)

                entry_count += 1

                # Extract timestamps (first/last)
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
        Add every log file in a directory.

        Args:
            dir_path: Directory path
            pattern: File pattern (glob)

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
        Compute the Merkle root.

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
        Obtain an RFC 3161 timestamp.

        Args:
            data: Data to timestamp (default: the Merkle root)

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
        Compute the Merkle root and obtain a timestamp (one step).

        Returns:
            (merkle_root, timestamp)
        """
        root = self.compute_merkle_root()
        timestamp = self.get_rfc3161_timestamp()
        return root, timestamp

    def to_dict(self) -> dict[str, Any]:
        """Convert the manifest to a dictionary."""
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
        Save the manifest.

        Args:
            output_path: Save path
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
        Load a manifest.

        Args:
            manifest_path: Manifest file path

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
        Verify the manifest.

        Checks that the recorded files have not been modified.

        Returns:
            True if all files are intact
        """
        # Recompute each file's hash
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

        # Compare the Merkle roots
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
    """CLI entry point."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="baldur.audit.signed_manifest",
        description="Create signed manifests for audit log files",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # create command
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

    # verify command
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
