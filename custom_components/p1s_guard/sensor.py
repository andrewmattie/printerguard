"""Minimal per-printer diagnostic sensors; no image or prompt is retained."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from . import DOMAIN

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    runtime=hass.data[DOMAIN]
    async_add_entities([GuardDiagnostic(runtime, printer) for printer in runtime.printers])

class GuardDiagnostic(SensorEntity):
    _attr_should_poll=False
    _attr_entity_category=EntityCategory.DIAGNOSTIC

    def __init__(self, runtime, printer):
        self.runtime=runtime
        self.printer=printer
        self._attr_name=f'{printer.name} Printer Guard'
        self._attr_unique_id=f'p1s_guard_{printer.prefix}_diagnostic'

    @property
    def native_value(self):
        return self.runtime.diagnostics[self.printer.prefix]['outcome']

    @property
    def extra_state_attributes(self):
        diagnostic=self.runtime.diagnostics[self.printer.prefix]
        return {key:diagnostic[key] for key in ('last_check','last_usable_assessment','check_started','assessment','notification','freshness','reason',
            'failure_phase','failure_category','failure_reason') if key in diagnostic}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        @callback
        def updated(name):
            if name == self.printer.prefix:
                self.async_write_ha_state()
        self.runtime.observers.append(updated)
        self.async_on_remove(lambda: self.runtime.observers.remove(updated))
