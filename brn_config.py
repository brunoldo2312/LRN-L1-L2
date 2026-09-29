"""
brn_config.py — Configuração externa (feature 12).
Prioridade: variáveis de ambiente > config.json > defaults.
"""
import os
import json
from pathlib import Path

CONFIG_FILE = "config.json"

DEFAULTS = {
    "web_port": 5000,
    "explorer_port": 8080,
    "p2p_port": 6001,
    "db_path": "brn_v2_chain.db",
    "read_only": False,
    "headless": False,
    "miner_enabled": False,
    "miner_interval": 5,
    "miner_whitelist": [],
    "upnp": True,
    "log_level": "INFO",
    "log_file": "",
    "tracker_url": "",
    "bootstrap_peers": [],
    "network_secret": "brunocoin-lan-2026",
    "web_user": "admin",
    "web_pass": "",
    "github": {
        "user": "",
        "repo": "",
        "file": "peers.json",
        "branch": "main",
        "token": "",
        "interval": 180,
    },
}

ENV_MAP = {
    "web_port":       "BRN_WEB_PORT",
    "explorer_port":  "BRN_EXPLORER_PORT",
    "p2p_port":       "BRN_P2P_PORT",
    "db_path":        "BRN_DB",
    "read_only":      "BRN_READ_ONLY",
    "headless":       "BRN_HEADLESS",
    "miner_interval": "BRN_MINER_INTERVAL",
    "upnp":           "BRN_UPNP",
    "log_level":      "BRN_LOG_LEVEL",
    "log_file":       "BRN_LOG_FILE",
    "tracker_url":    "BRN_TRACKER",
    "web_user":       "BRN_WEB_USER",
    "web_pass":       "BRN_WEB_PASS",
    "network_secret": "BRN_NETWORK_SECRET",
}

GH_ENV = {
    "user":     "BRN_GH_USER",
    "repo":     "BRN_GH_REPO",
    "file":     "BRN_GH_FILE",
    "branch":   "BRN_GH_BRANCH",
    "token":    "BRN_GH_TOKEN",
    "interval": "BRN_GH_INTERVAL",
}


def _bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "sim")


def _int(v, default=0):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


class Config:
    def __init__(self, path=CONFIG_FILE):
        self.path = Path(path)
        self.source = "defaults"
        self.data = dict(DEFAULTS)
        self.data["github"] = dict(DEFAULTS["github"])
        self._load_file()
        self._load_env()

    def _load_file(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for k, v in loaded.items():
                    if k == "github" and isinstance(v, dict):
                        self.data["github"].update(v)
                    else:
                        self.data[k] = v
                self.source = str(self.path)
        except Exception as e:
            print(f"[Config] aviso: falha ao ler {self.path}: {e}")

    def _load_env(self):
        for key, env_name in ENV_MAP.items():
            val = os.environ.get(env_name)
            if val is None:
                continue
            if isinstance(DEFAULTS.get(key), bool):
                self.data[key] = _bool(val)
            elif isinstance(DEFAULTS.get(key), int):
                self.data[key] = _int(val, self.data[key])
            else:
                self.data[key] = val
            self.source = "config.json + env"
        for key, env_name in GH_ENV.items():
            val = os.environ.get(env_name)
            if val is None:
                continue
            if key == "interval":
                self.data["github"][key] = _int(val, 180)
            else:
                self.data["github"][key] = val
            self.source = "config.json + env"
        bs = os.environ.get("BRN_BOOTSTRAP_PEERS")
        if bs:
            self.data["bootstrap_peers"] = [p.strip() for p in bs.split(",") if p.strip()]
        wl = os.environ.get("BRN_MINER_WHITELIST")
        if wl is not None:
            self.data["miner_whitelist"] = [p.strip() for p in wl.split(",") if p.strip()]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def apply_to_env(self):
        """Propaga config para variáveis de ambiente (para server.py/explorer.py/p2p lerem)."""
        mapping = {
            "web_port":       "BRN_WEB_PORT",
            "explorer_port":  "BRN_EXPLORER_PORT",
            "p2p_port":       "BRN_P2P_PORT",
            "db_path":        "BRN_DB",
            "tracker_url":    "BRN_TRACKER",
            "network_secret": "BRN_NETWORK_SECRET",
            "web_user":       "BRN_WEB_USER",
            "web_pass":       "BRN_WEB_PASS",
        }
        for key, env_name in mapping.items():
            val = self.data.get(key)
            if val is not None and str(val) != "":
                os.environ[env_name] = str(val)
        if self.data["read_only"]:
            os.environ["BRN_READ_ONLY"] = "1"
        if self.data["github"]["user"]:
            for k, env_name in GH_ENV.items():
                v = self.data["github"].get(k)
                if v is not None and str(v) != "":
                    os.environ[env_name] = str(v)
        if self.data["bootstrap_peers"]:
            os.environ["BRN_BOOTSTRAP_PEERS"] = ",".join(self.data["bootstrap_peers"])
        if self.data["miner_whitelist"]:
            os.environ["BRN_MINER_WHITELIST"] = ",".join(self.data["miner_whitelist"])
        if not self.data["upnp"]:
            os.environ["BRN_UPNP"] = "0"

    def to_dict(self):
        return dict(self.data)