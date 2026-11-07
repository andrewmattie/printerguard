"""Explicit, dependency-free configuration validation; no household defaults."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Printer:
    name: str
    prefix: str
    camera: str

    def entity(self, domain: str, suffix: str) -> str:
        return f'{domain}.{self.prefix}_{suffix}'


@dataclass(frozen=True)
class Settings:
    ai_task: str
    notify_service: str
    printers: tuple[Printer, ...]
    screen_ai_task: str | None = None


def _entity(value, domain: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf'{domain}\.[a-z0-9_]+', value):
        raise ValueError(f'expected a {domain} entity or service')
    return value


def parse_config(value) -> Settings:
    """Reject incomplete, ambiguous or unknown settings before any monitoring.

    Error messages deliberately exclude submitted values. Printer prefixes retain
    the prototype's entity-suffix convention; camera IDs are always explicit.
    """
    required = {'ai_task', 'notify_service', 'printers'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {'screen_ai_task'}):
        raise ValueError('ai_task, notify_service and printers are required')
    ai_task = _entity(value['ai_task'], 'ai_task')
    screen_ai_task = _entity(value['screen_ai_task'], 'ai_task') if 'screen_ai_task' in value else None
    notify_service = _entity(value['notify_service'], 'notify')
    entries = value['printers']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2:
        raise ValueError('configure one or two printers')
    printers = []
    prefixes = set()
    cameras = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'name', 'entity_prefix', 'camera'}:
            raise ValueError('each printer requires name, entity_prefix and camera')
        name = entry['name']
        prefix = entry['entity_prefix']
        camera = _entity(entry['camera'], 'camera')
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValueError('printer name must be a short, printable string')
        if not isinstance(prefix, str) or not re.fullmatch(r'[a-z0-9_]+', prefix):
            raise ValueError('invalid printer entity prefix')
        if prefix in prefixes or camera in cameras:
            raise ValueError('printer prefixes and cameras must be distinct')
        prefixes.add(prefix)
        cameras.add(camera)
        printers.append(Printer(name, prefix, camera))
    return Settings(ai_task, notify_service, tuple(printers), screen_ai_task)
