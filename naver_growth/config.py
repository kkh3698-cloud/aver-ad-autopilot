"""설정 로딩. config.yaml(비밀값 제외) + 환경변수(.env, 비밀값)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _load_dotenv(path: Path) -> None:
    """python-dotenv 없이 .env 읽기. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    raw: dict[str, Any]
    base_dir: Path
    demo: bool = False
    secrets: dict[str, str | None] = field(default_factory=dict)

    # 편의 접근자 -------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def db_path(self) -> Path:
        return self.base_dir / self.get("storage.db_path", "data/autopilot.db")

    @property
    def plans_dir(self) -> Path:
        return self.base_dir / self.get("storage.plans_dir", "data/plans")

    @property
    def reports_dir(self) -> Path:
        return self.base_dir / self.get("storage.reports_dir", "data/reports")

    @property
    def dry_run(self) -> bool:
        return bool(self.get("ads.dry_run", True))

    @property
    def kill_switch(self) -> Path:
        return self.base_dir / self.get("ads.guardrails.kill_switch_file", "STOP")


SECRET_KEYS = [
    "NAVER_AD_API_KEY",
    "NAVER_AD_SECRET_KEY",
    "NAVER_AD_CUSTOMER_ID",
    "NAVER_OPENAPI_CLIENT_ID",
    "NAVER_OPENAPI_CLIENT_SECRET",
    "NAVER_APIHUB_KEY_ID",
    "NAVER_APIHUB_KEY",
    "ANTHROPIC_API_KEY",
    "YOUTUBE_API_KEY",
    "SLACK_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


def load_config(path: str | Path = "config.yaml", demo: bool = False) -> Config:
    path = Path(path)
    if not path.exists():
        example = Path(__file__).resolve().parent.parent / "config.example.yaml"
        if demo and example.exists():
            path = example
        else:
            raise FileNotFoundError(
                f"{path} 가 없습니다. config.example.yaml 을 복사해 config.yaml 로 만드세요."
            )
    base_dir = path.resolve().parent
    _load_dotenv(base_dir / ".env")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config(raw=raw, base_dir=base_dir, demo=demo)
    cfg.secrets = {k: os.environ.get(k) for k in SECRET_KEYS}
    if demo:
        # 데모는 별도 DB를 쓰고 절대 실제 API를 건드리지 않는다.
        cfg.raw.setdefault("storage", {})
        cfg.raw["storage"]["db_path"] = "data/demo.db"
        cfg.raw["storage"]["plans_dir"] = "data/demo_plans"
        cfg.raw["storage"]["reports_dir"] = "data/demo_reports"
    for d in (cfg.db_path.parent, cfg.plans_dir, cfg.reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
