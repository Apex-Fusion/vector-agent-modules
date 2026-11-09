"""Durable storage of commitments: the salt, the vote and the record of each
commit attempt, written before the commit is sent (SPEC §13 step 1.5).

One file per (network, jury deployment, case, juror token)::

    <root>/<network>/<J hex>/<case token hex>/<juror token hex>.json

The file holds every attempt (``vector-jury-commit/1``). A reveal reads the
commitment of the juror's ballot from the chain and takes the attempt with
that commitment, so a retried commit never loses the salt of an earlier
attempt that may still land. Directories have mode 700 and files mode 600.
"""

import base64
import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from . import rules
from .documents import doc_hash
from .types import vote_name

FORMAT = "vector-jury-commit/1"
#: The default root (``--state-dir`` of the command line tool).
DEFAULT_ROOT = Path.home() / ".vector-jury" / "commits"


@dataclasses.dataclass
class Attempt:
    agent: bytes
    vote: str
    salt: bytes
    record: bytes
    record_media: str
    commit: bytes
    created: str
    tx: Optional[str] = None

    @property
    def record_hash(self) -> bytes:
        return doc_hash(self.record)

    def to_json(self) -> Dict[str, Any]:
        return {
            "agent": self.agent.hex(),
            "vote": self.vote,
            "salt": self.salt.hex(),
            "record": base64.b64encode(self.record).decode("ascii"),
            "record_hash": self.record_hash.hex(),
            "record_media": self.record_media,
            "commit": self.commit.hex(),
            "created": self.created,
            "tx": self.tx,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Attempt":
        attempt = cls(
            agent=bytes.fromhex(data["agent"]),
            vote=vote_name(data["vote"]),
            salt=bytes.fromhex(data["salt"]),
            record=base64.b64decode(data["record"]),
            record_media=data.get("record_media", "application/json"),
            commit=bytes.fromhex(data["commit"]),
            created=data.get("created", ""),
            tx=data.get("tx"),
        )
        if bytes.fromhex(data.get("record_hash", attempt.record_hash.hex())) != attempt.record_hash:
            raise ValueError("the stored record does not match its hash")
        return attempt


def make_attempt(case: bytes, agent: bytes, vote, record: bytes, *, salt: Optional[bytes] = None,
                 record_media: str = "application/json", created: str = "") -> Attempt:
    """A new attempt with a fresh salt (or ``salt``) and its commitment."""
    salt = rules.new_salt() if salt is None else bytes(salt)
    commit = rules.commit_hash(case, agent, vote, salt, doc_hash(record))
    return Attempt(bytes(agent), vote_name(vote), salt, bytes(record), record_media, commit, created)


def _hex(value: Union[bytes, str]) -> str:
    return value.hex() if isinstance(value, (bytes, bytearray)) else str(value).lower()


class CommitStore:
    def __init__(self, root: Union[str, Path, None] = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT

    def file(self, network: str, jury: Union[bytes, str], case: Union[bytes, str], juror: Union[bytes, str]) -> Path:
        return self.root / network / _hex(jury) / _hex(case) / f"{_hex(juror)}.json"

    def attempts(self, network, jury, case, juror) -> List[Attempt]:
        path = self.file(network, jury, case, juror)
        if not path.exists():
            return []
        data = json.loads(path.read_text())
        if data.get("format") != FORMAT:
            raise ValueError(f"{path} is not a {FORMAT} file")
        return [Attempt.from_json(a) for a in data.get("attempts", [])]

    def find(self, network, jury, case, juror, commit: bytes) -> Optional[Attempt]:
        return next((a for a in self.attempts(network, jury, case, juror) if a.commit == bytes(commit)), None)

    def matching(self, network, jury, case, juror, vote, record: bytes) -> Optional[Attempt]:
        """An earlier attempt with the same vote and record (its salt can be
        used again, so a retry keeps the same commitment)."""
        name = vote_name(vote)
        return next((a for a in self.attempts(network, jury, case, juror)
                     if a.vote == name and a.record == bytes(record)), None)

    def add(self, network, jury, case, juror, attempt: Attempt) -> Path:
        """Store an attempt durably (file and directory synced) before the
        commit goes out. An attempt with the same commitment is replaced."""
        attempts = [a for a in self.attempts(network, jury, case, juror) if a.commit != attempt.commit]
        attempts.append(attempt)
        return self._write(network, jury, case, juror, attempts)

    def mark_sent(self, network, jury, case, juror, commit: bytes, tx_id: str) -> Path:
        attempts = self.attempts(network, jury, case, juror)
        for a in attempts:
            if a.commit == bytes(commit):
                a.tx = tx_id
        return self._write(network, jury, case, juror, attempts)

    def _directory(self, path: Path) -> None:
        """Create the missing directories down to the parent of ``path`` with
        mode 700 (also the missing ancestors of the root), and set mode 700 on
        the directories below the root."""
        missing, current = [], self.root
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)
        current = self.root
        for part in path.parent.relative_to(self.root).parts:
            current = current / part
            current.mkdir(mode=0o700, exist_ok=True)
            os.chmod(current, 0o700)

    def _write(self, network, jury, case, juror, attempts: List[Attempt]) -> Path:
        path = self.file(network, jury, case, juror)
        self._directory(path)
        data = {"format": FORMAT, "network": network, "jury": _hex(jury), "case": _hex(case), "juror": _hex(juror),
                "attempts": [a.to_json() for a in attempts]}
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return path
