"""Device configuration: KEY=VALUE file (default /etc/hbpipe/pipeline.env), overridden by env vars."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass

DEFAULT_CONFIG = "/etc/hbpipe/pipeline.env"


@dataclass
class Config:
    mock_host: str = "127.0.0.1"
    mock_port: int = 7100
    data_dir: str = "/var/lib/hbpipe"
    cloud_url: str = ""
    cloud_token: str = ""
    device_id: str = socket.gethostname()
    poll_interval: float = 0.1      # seconds between reads of one sensor
    commit_interval: float = 0.2    # seconds between durable commits (bounds power-cut loss)
    reply_timeout: float = 2.0
    reserve_mb: int = 16            # disk space kept back for loss records when the disk fills
    low_water_mb: int = 24          # enter disk-full mode below this much free space

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "pipeline.db")

    @classmethod
    def load(cls) -> "Config":
        values = {}
        path = os.environ.get("HB_CONFIG", DEFAULT_CONFIG)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        values[k.strip()] = v.strip().strip('"')
        values.update({k: v for k, v in os.environ.items() if k.startswith("HB_")})
        cfg = cls()
        for name, field_type in cls.__annotations__.items():
            key = "HB_" + name.upper()
            if key in values:
                raw = values[key]
                kind = type(getattr(cfg, name))
                setattr(cfg, name, kind(raw))
        return cfg
