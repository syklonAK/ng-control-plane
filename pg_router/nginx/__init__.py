"""Nginx layer: detection, management, installation and config generation."""

from . import modules
from .generator import ConfigGenerator, GenerationResult, generate_config
from .installer import InstallReport, NginxInstaller
from .manager import NginxManager, NginxStatus
from .modules import NginxModules, detect as detect_nginx

__all__ = [
    "ConfigGenerator",
    "GenerationResult",
    "InstallReport",
    "NginxInstaller",
    "NginxManager",
    "NginxModules",
    "NginxStatus",
    "detect_nginx",
    "generate_config",
    "modules",
]
