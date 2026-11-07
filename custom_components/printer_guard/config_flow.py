"""Read-only UI configuration; YAML remains a separate authoritative source."""
from __future__ import annotations

from pathlib import Path
import os

import probatio
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import DOMAIN, RESOURCES
from .config import OPTIONAL_ENTITIES, REQUIRED_ENTITIES, parse_config


def ownership_error(hass, settings, entry_id=None):
    """Reserve identities and bindings, including disabled/unloaded UI entries."""
    owners=[]
    if yaml_settings := hass.data.get(RESOURCES,{}).get('yaml'):
        owners.append(yaml_settings)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == entry_id or entry.source == 'ignore':
            continue
        try:
            owners.append(parse_config(dict(entry.options or entry.data),printer_id=entry.unique_id))
        except ValueError:
            continue
    # Failed unloads can leave the previous runtime active while options have
    # already been saved. Reserve its actual bindings too, not only new options.
    active=[runtime for key,runtime in hass.data.get(RESOURCES,{}).get('entries',{}).items()
        if key != entry_id]
    if any(left.key == right.key or left.bindings & right.bindings
            for owner in owners+active for left in owner.printers for right in settings.printers):
        return 'printer_conflict'
    return None


def validate_input(hass, value, printer_id=None, entry_id=None, previous=None):
    """Validate without capturing images, invoking providers or sending alerts."""
    printer={key:value.get(key) for key in ('name',*REQUIRED_ENTITIES)}
    printer.update({key:value[key] for key in OPTIONAL_ENTITIES if value.get(key)})
    data={key:value.get(key) for key in ('ai_task','notify_service')}
    data['printers']=[printer]
    if value.get('screen_ai_task'):
        data['screen_ai_task']=value['screen_ai_task']
    try:
        settings=parse_config(data,printer_id=printer_id)
    except ValueError:
        return None,{'base':'invalid_config'}
    if error := ownership_error(hass,settings,entry_id):
        return None,{'base':error}
    media=hass.config.media_dirs.get('local')
    if not media or not Path(media).is_absolute() or not Path(media).is_dir() or not os.access(media,os.W_OK | os.X_OK):
        return None,{'base':'missing_media'}
    for field in (*REQUIRED_ENTITIES,*OPTIONAL_ENTITIES,'ai_task','screen_ai_task'):
        entity=printer.get(field) if field in REQUIRED_ENTITIES | OPTIONAL_ENTITIES else data.get(field)
        if entity is None:
            continue
        state=hass.states.get(entity)
        if state is None:
            # Legacy setups could omit telemetry sensors. Preserve unchanged
            # optional bindings during unrelated edits; new selections must exist.
            if field in OPTIONAL_ENTITIES and previous and previous.get(field) == entity:
                continue
            return None,{field:'missing_entity'}
        if field in ('camera','ai_task','screen_ai_task') and state.state in ('unavailable','unknown'):
            return None,{field:'unavailable_entity'}
        if field in ('ai_task','screen_ai_task'):
            from homeassistant.components.ai_task import AITaskEntityFeature
            needed=AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
            features=state.attributes.get('supported_features',0)
            if not isinstance(features,int) or features & needed != needed:
                return None,{field:'unsupported_ai_task'}
    if not hass.services.has_service('notify',settings.notify_service.split('.',1)[1]):
        return None,{'notify_service':'missing_notify_service'}
    return data,{}


def form_schema(hass, current=None):
    """Entity selectors for cameras/tasks, service choices and explicit bindings."""
    current=current or {}
    fields={}
    for key in ('name',*REQUIRED_ENTITIES,*OPTIONAL_ENTITIES,'ai_task','screen_ai_task','notify_service'):
        optional=key in OPTIONAL_ENTITIES or key == 'screen_ai_task'
        marker=probatio.Optional if optional else probatio.Required
        options={}
        if key in current:
            options=({'description':{'suggested_value':current[key]}} if optional
                else {'default':current[key]})
        field=marker(key,**options)
        if key in REQUIRED_ENTITIES | OPTIONAL_ENTITIES or key in ('ai_task','screen_ai_task'):
            domain=(REQUIRED_ENTITIES | OPTIONAL_ENTITIES).get(key,'ai_task')
            fields[field]=selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))
        elif key == 'notify_service':
            services=hass.services.async_services().get('notify',{})
            fields[field]=selector.SelectSelector(selector.SelectSelectorConfig(
                options=sorted('notify.'+service for service in services),custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN))
        else:
            fields[field]=selector.TextSelector()
    return probatio.Schema(fields)


class PrinterGuardConfigFlow(ConfigFlow, domain=DOMAIN):
    """One printer per UI entry, with independent AI and notification routing."""
    VERSION=2

    async def async_step_user(self,user_input=None):
        errors={}
        if user_input is not None:
            data,errors=validate_input(self.hass,user_input)
            if not errors:
                await self.async_set_unique_id(parse_config(data).printers[0].key)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=data['printers'][0]['name'],data=data)
        return self.async_show_form(step_id='user',data_schema=form_schema(self.hass,user_input),errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PrinterGuardOptionsFlow()


class PrinterGuardOptionsFlow(OptionsFlow):
    """Edit explicit bindings without mutating the saved printer identity."""
    async def async_step_init(self,user_input=None):
        entry=self.config_entry
        current=dict(entry.options or entry.data)
        printer=current['printers'][0]
        errors={}
        if user_input is not None:
            data,errors=validate_input(self.hass,user_input,entry.unique_id,entry.entry_id,printer)
            if not errors:
                return self.async_create_entry(title='',data=data)
        values={key:current[key] for key in ('ai_task','notify_service','screen_ai_task') if key in current}
        values.update(printer)
        if user_input is not None:
            values=user_input
        return self.async_show_form(step_id='init',data_schema=form_schema(self.hass,values),errors=errors)
