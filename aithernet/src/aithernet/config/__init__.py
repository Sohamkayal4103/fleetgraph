"""Node configuration: typed settings model and YAML/environment loader."""

from aithernet.config.loader import DEFAULT_CONFIG_PATH, load_config
from aithernet.config.settings import NodeConfig

__all__ = ["NodeConfig", "load_config", "DEFAULT_CONFIG_PATH"]
