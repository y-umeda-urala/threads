"""投稿済みの記録。二重投稿を防ぐための唯一の判断材料。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = 1


@dataclass
class State:
    path: Path
    posted: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "State":
        file_path = Path(path)
        if not file_path.exists():
            return cls(path=file_path)
        raw = json.loads(file_path.read_text(encoding="utf-8") or "{}")
        return cls(path=file_path, posted=raw.get("posted", {}))

    @property
    def posted_ids(self) -> set[str]:
        return set(self.posted)

    def record(
        self,
        item_id: str,
        *,
        post_id: str,
        permalink: str | None = None,
        posted_at: datetime | None = None,
    ) -> None:
        self.posted[item_id] = {
            "post_id": post_id,
            "permalink": permalink,
            "posted_at": (posted_at or datetime.now(timezone.utc)).isoformat(),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STATE_VERSION, "posted": self.posted}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
