"""Plugin auto-discovery.

Scans this package for all MCPPlugin subclasses and instantiates them.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from plugin_base import MCPPlugin


def discover_plugins() -> list[MCPPlugin]:
    """Import all modules in this package and return instances of MCPPlugin subclasses."""
    package_dir = Path(__file__).parent
    instances: list[MCPPlugin] = []
    seen: set[str] = set()

    for info in pkgutil.walk_packages([str(package_dir)], prefix="plugins."):
        try:
            mod = importlib.import_module(info.name)
        except Exception as exc:
            print(f"[plugins] failed to import {info.name}: {exc}", flush=True)
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, MCPPlugin)
                and obj is not MCPPlugin
                and obj.__name__ not in seen
            ):
                try:
                    instance = obj()
                    if instance.name:
                        instances.append(instance)
                        seen.add(obj.__name__)
                        print(f"[plugins] loaded {instance.name} ({len(instance.tools)} tools)", flush=True)
                except Exception as exc:
                    print(f"[plugins] failed to instantiate {obj.__name__}: {exc}", flush=True)

    return instances
