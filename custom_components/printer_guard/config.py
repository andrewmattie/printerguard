"""Explicit, dependency-free configuration validation; no household defaults."""
from __future__ import annotations

from dataclasses import dataclass
import re


# Keys match the YAML fields, UI selectors and runtime adapter.
REQUIRED_ENTITIES = {
    'camera': 'camera',
    'print_status': 'sensor',
    'current_stage': 'sensor',
    'online': 'binary_sensor',
    'print_error': 'binary_sensor',
    'task_name': 'sensor',
    'start_time': 'sensor',
}
OPTIONAL_ENTITIES = {
    'current_layer': 'sensor',
    'total_layer_count': 'sensor',
    'print_progress': 'sensor',
    'nozzle_temperature': 'sensor',
    'bed_temperature': 'sensor',
}


@dataclass(frozen=True)
class Printer:
    name: str
    key: str  # Stable runtime/diagnostic identity, never used to infer bindings.
    camera: str
    print_status: str
    current_stage: str
    online: str
    print_error: str
    task_name: str
    start_time: str
    current_layer: str | None = None
    total_layer_count: str | None = None
    print_progress: str | None = None
    nozzle_temperature: str | None = None
    bed_temperature: str | None = None

    @property
    def bindings(self) -> frozenset[str]:
        return frozenset(entity for key in REQUIRED_ENTITIES | OPTIONAL_ENTITIES
            if (entity := getattr(self, key)) is not None)


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


def parse_config(value, *, printer_id=None) -> Settings:
    """Reject incomplete, ambiguous or unknown settings before any monitoring.

    Legacy prefix-only YAML remains readable without rewriting it. All runtime
    bindings are explicit, including those expanded from legacy configuration.
    UI entries supply their saved identity so edits cannot rename diagnostics.
    Error messages deliberately exclude submitted values.
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
    if printer_id is not None and (len(entries) != 1 or not isinstance(printer_id, str)
            or not re.fullmatch(r'[a-z0-9_.]+', printer_id)):
        raise ValueError('invalid stored printer identity')
    printers = []
    identities = set()
    bindings = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('each printer requires a name and explicit entities')
        name = entry.get('name')
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValueError('printer name must be a short, printable string')
        if 'entity_prefix' in entry:
            if set(entry) != {'name', 'entity_prefix', 'camera'}:
                raise ValueError('legacy prefixes cannot be mixed with explicit entities')
            prefix = entry['entity_prefix']
            if not isinstance(prefix, str) or not re.fullmatch(r'[a-z0-9_]+', prefix):
                raise ValueError('invalid legacy printer entity prefix')
            entities = {key: _entity(entry['camera'], 'camera') if key == 'camera'
                else f'{domain}.{prefix}_{key}'
                for key, domain in (REQUIRED_ENTITIES | OPTIONAL_ENTITIES).items()}
            identity = prefix
        else:
            if not {'name', *REQUIRED_ENTITIES} <= set(entry) or set(entry) - {'name', *REQUIRED_ENTITIES, *OPTIONAL_ENTITIES}:
                raise ValueError('each printer requires a name and explicit entities')
            entities = {key: _entity(entry[key], domain)
                for key, domain in (REQUIRED_ENTITIES | OPTIONAL_ENTITIES).items() if key in entry}
            status = entities['print_status']
            # Preserve the old diagnostic/latch identity when replacing a legacy
            # prefix with exactly its original explicit print-status binding.
            identity = (status.removeprefix('sensor.').removesuffix('_print_status')
                if status.endswith('_print_status') and status != 'sensor._print_status' else status)
        identity = printer_id if printer_id is not None else identity
        selected = set(entities.values())
        if len(selected) != len(entities):
            raise ValueError('printer entity bindings must be distinct')
        if identity in identities or selected & bindings:
            raise ValueError('printer identities and entity bindings must be distinct')
        identities.add(identity)
        bindings.update(selected)
        printers.append(Printer(name=name, key=identity, **entities))
    return Settings(ai_task, notify_service, tuple(printers), screen_ai_task)
