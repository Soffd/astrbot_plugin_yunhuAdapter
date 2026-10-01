"""从插件根目录的 AstrBot 配置 schema 读取默认值。"""

import json
from pathlib import Path

with open(
    Path(__file__).resolve().parents[1] / "_conf_schema.json", encoding="utf-8"
) as schema_file:
    CONFIG_METADATA = json.load(schema_file)
DEFAULT_CONFIG = {key: value["default"] for key, value in CONFIG_METADATA.items()}
