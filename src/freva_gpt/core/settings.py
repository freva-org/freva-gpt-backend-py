from __future__ import annotations

import os
import re
import sysconfig
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    MutableMapping,
    Optional,
    Tuple,
    Type,
    TypeAlias,
    TypedDict,
    Union,
)

import toml
from cached_property import cached_property
from platformdirs import user_config_path
from pydantic import BaseModel

ENV_PREFIX: str = "FREVAGPT"
InnerConfig: TypeAlias = Union[int, float, str, bool, None]
ConfigValue: TypeAlias = Union[InnerConfig, List[InnerConfig]]


def _conv_to_int(var: Optional[str], fallback: int) -> int:
    """Convert a env variable to a dict."""
    var = var or ""
    if not var.isdigit():
        return fallback
    return int(var)


def _str_to_bool(
    value: Optional[str], default: Optional[str] = None
) -> Optional[bool]:
    value = value or default
    if value is None:
        return None
    return value.lower() in ("1", "true", "yes")


def _str_to_list(value: Optional[str]) -> List[str]:
    value = value or ""
    return [v.strip() for v in value.split(",") if v.strip()] or []


def _merge_config(
    user_config: MutableMapping[str, Any],
    system_config: MutableMapping[str, Any],
) -> None:
    for key, value in system_config.items():
        if key in user_config and isinstance(value, dict):
            _merge_config(user_config[key], system_config[key])
        else:
            user_config.setdefault(key, value)


class CliArgs(TypedDict):
    """Arguments for parsing the command line interface."""

    name: str
    nargs: Optional[Union[str, int]]
    help: str
    default: str
    type: Type


class Types(Enum):
    """Types supported by the config."""

    string = str
    integer = int
    float = float
    bool = bool

    @classmethod
    def items(cls) -> List[str]:
        return list(cls.__members__)

    @classmethod
    def get_type(cls, t: ConfigValue, length: int = 0) -> str:
        """Get the basetype from a givven input type.

        The types can be either a base type or a collection of that containes
        *one* base type. Dictionaries and Collections that contain multiple
        types are not suppored.
        """
        if t is None:
            raise ValueError("NoneTypes are not allowed.")
        if isinstance(t, (list, set, tuple)):
            if len(t) == 0:
                raise ValueError("Can't deduce type from collection")
            return cls.get_type(t[0], length=len(t))
        mapping = {v.value: k for (k, v) in Types.__members__.items()}
        _type = mapping[type(t)]
        if length > 0:
            return f"{_type}[{length}]"
        return _type

        return mapping[t]


class ParseType(TypedDict):
    base: str
    length: Optional[int]
    multi_valued: bool


class Config(BaseModel):
    """Base model for each config value."""

    name: str
    """Name of the config key."""
    default: ConfigValue = None
    """The assigned default value."""
    description: str = ""
    """Human readable description."""
    type: str = "string"
    """The data type."""

    def model_post_init(self, __context: Any = None) -> None:
        env_var = f"{ENV_PREFIX}_{self.name.upper()}"
        self.__parsed_type = self.parse_type(self.type)
        value = os.getenv(env_var)
        if value:
            _type = getattr(Types, self.__parsed_type["base"])
            if self.__parsed_type["multi_valued"]:
                self.default = map(
                    _type, [k.strip() for k in value.split(",") if k.strip()]
                )
            else:
                self.default = _type.value(value.strip())

    @classmethod
    def deduce_type(cls, name: str, default: ConfigValue) -> "Config":
        """Create an instance of the config class by guessing the type."""
        print(default, Types.get_type(default))
        return cls(name=name, default=default, type=Types.get_type(default))

    @classmethod
    def parse_type(cls, v: str) -> ParseType:
        """Parse the data types.

        Accepts
        ^^^^^^^
          - 'string', 'integer', 'float'  -> length=None
          - 'float[2]', 'integer[5]' -> length=number
          - 'string[]'  -> length=None, multi_valued semantics
        """
        m = re.fullmatch(
            r"({})(\[(\d*)\])?".format("|".join(Types.items())), v
        )
        if not m:
            raise ValueError(f"invalid type spec {v!r}")
        base, _, num = m.groups()
        return {
            "base": base,
            "length": int(num) if num else None,
            "multi_valued": ("[" in v and "]" in v),
        }

    def to_env(self) -> Tuple[str, str]:
        """Create an environment entry."""
        key = f"{ENV_PREFIX}_{self.name.upper()}"
        if self.__parsed_type["multi_valued"]:
            value = ",".join(map(str, self.default))
        elif self.__parsed_type["base"] == "bool":
            value = str(int(self.default))
        else:
            value = str(self.default)
        return key, value

    @property
    def cli_params(self) -> CliArgs:
        if self.__parsed_type["multi_valued"]:
            default = " ".join(map(str, self.default))
            nargs = "*"
        else:
            default = self.default
            nargs = 1
        args = {
            "name": f"--{self.name.replace('_', '-')}",
            "help": self.description,
            "type": getattr(Types, self.__parsed_type["base"]).value,
            "default": default,
            "nargs": nargs,
        }
        if self.type == "bool":
            args.pop("type")
            args.pop("nargs")
            args["action"] = "store_true"
        return args


class BootstrapConfig:
    """Bootstrap Configuration class."""

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
        **extra_config: ConfigValue,
    ):
        self.config_path = config_path or self.get_default_config_path()
        try:
            cfg = toml.loads(self.config_path.read_text())
        except (FileNotFoundError, IsADirectoryError, toml.TomlDecodeError):
            self.config_path.parent.mkdir(exist_ok=True, parents=True)
            self.config_path.write_text(
                self.system_config_file().read_text(), encoding="utf-8"
            )
            cfg = toml.loads(self.config_path.read_text())

        self._cfg: Dict[str, Config] = {
            k: Config.deduce_type(name=k, default=v)
            for (k, v) in extra_config.items()
        }
        _merge_config(cfg, self.default_config)

    @staticmethod
    def get_default_config_path():
        config_prefix = f"{ENV_PREFIX}_CONFIG_PATH"
        sys_config_path = (
            Path(sysconfig.get_paths()["data"])
            / "share"
            / f"{ENV_PREFIX.lower()}"
            / "server_settings.toml"
        )
        config_path = os.getenv(config_prefix) or sys_config_path
        if os.access(config_path, os.W_OK):
            return config_path
        return (
            user_config_path("freva-gpt", ensure_exists=True)
            / "server_settings.toml"
        )

    @classmethod
    def from_env(
        cls, key: str, default: Optional[str] = None
    ) -> Optional[str]:
        """Read a config entry from environment."""
        key = ENV_PREFIX.rstrip("_") + "_" + key.upper()
        return os.getenv(key, default)

    @classmethod
    def from_cli_env(
        cls,
        config_path: Optional[Union[str, Path]] = None,
        **kwargs: ConfigValue,
    ) -> "BootstrapConfig":
        config_path = config_path or os.getenv(f"{ENV_PREFIX}_CONFIG_PATH")
        return cls(
            config_path=Path(config_path or cls.system_config_file()), **kwargs
        )

    @staticmethod
    def system_config_file() -> Path:
        """Check the system config."""
        return Path(__file__).parent / "server_settings.toml"

    @cached_property
    def default_config(self) -> Dict[str, Config]:
        """Read the systems default config."""
        user_cfg = toml.loads(self.config_path.read_text())
        system_cfg = toml.loads(self.system_config_file().read_text())
        _merge_config(user_cfg, system_cfg)
        for key, _cfg in user_cfg.items():
            self._cfg.setdefault(key, Config(name=key, **_cfg))
        self._cfg["dev_mode"] = Config(
            name="dev_mode",
            description="Enable development mode",
            type="bool",
            default=False,
        )
        return self._cfg


class Settings:
    """Setup for the Server config."""

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
        **kwargs: ConfigValue,
    ) -> None:
        self.model_fields = list(
            BootstrapConfig.from_cli_env(
                config_path, **kwargs
            ).default_config.values()
        )
        for cfg in self.model_fields:
            setattr(self, cfg.name, cfg.default)
            setattr(self, cfg.name.upper(), cfg.default)
        for cfg, value in kwargs.items():
            setattr(self, cfg, value)
            setattr(self, cfg.upper(), value)


# Simple singleton-style accessor
_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
    return _SETTINGS


def get_server_url_dict(server_list):
    url_dict: Dict[str:str] = {}
    for s in server_list:
        s_url = os.getenv(f"{s.upper()}_SERVER_URL", "")
        if s_url:
            url_dict.update({s: s_url})
        else:
            ValueError(f"Please set url address for MCP server {s}!")
    return url_dict
