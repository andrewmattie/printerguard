"""Monitor-only integration. Physical pause is deliberately unavailable."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import time

from .ai_task_adapter import AITaskAdapter
from .config import parse_config
from .engine import FRAME_FAILURE_REASONS, Monitor, safe_error_category
from .ha_runtime import camera_capture, context_from_states
from .latch import Latch
from .media import Staging

DOMAIN='p1s_guard'
_LOGGER=logging.getLogger(__name__)

def CONFIG_SCHEMA(config):
    """Validate YAML through HA's schema hook without importing HA in unit tests."""
    import probatio
    if DOMAIN in config:
        try:
            parse_config(config[DOMAIN])
        except ValueError:
            raise probatio.Invalid('Invalid p1s_guard configuration; see the repository README') from None
    return config

def diagnostic_outcome(result):
    if result == 'screen_clear_or_unjudgeable': return 'screen_success'
    if result in ('monitor_only', 'comparison_clear_or_unjudgeable'): return 'comparison_success'
    if result in ('ineligible', 'session_changed', 'stale_result', 'stale_gap', 'busy', 'already_attempted'): return 'skipped'
    return 'failure'

class Runtime:
    def __init__(self,hass,root,latch,settings,staging=None):
        self.hass=hass
        self.root=root
        self.latch=latch
        self.printers=settings.printers
        self.staging=staging or Staging(root)
        self.started=time.monotonic()
        self.jobs={}
        self.tasks=set()
        self.last_notice={}
        self.stopping=False
        self.unsub=None
        self.diagnostics={p.prefix: {'outcome':'not_checked', 'last_check':None} for p in self.printers}
        self.diagnostic_last_publish={}
        self.observers=[]
        for printer in self.printers:
            ai=AITaskAdapter(hass,settings.ai_task,self.staging)
            async def capture(p=printer): return await camera_capture(hass,p)
            def context(p=printer): return context_from_states(hass.states,p)
            async def reserve(task,start,p=printer): return await latch.reserve(p.prefix,task,start)
            async def pause(): raise RuntimeError('automatic pause is disabled')
            async def notify(message,p=printer):
                now=time.monotonic()
                if now-self.last_notice.get(p.prefix,float('-inf')) < 1800: return
                await hass.services.async_call('notify',settings.notify_service.split('.',1)[1],
                    {'title':f'{p.name} print check', 'message':message},blocking=True)
                self.last_notice[p.prefix]=now
            self.jobs[printer.prefix]=Monitor(capture=capture,screen=ai.screen,compare=ai.compare,
                context=context,reserve=reserve,pause=pause,notify=notify,clock=time.monotonic,allow_pause=False,
                ai_phase=lambda adapter=ai: adapter.diagnostic_phase)

    def tick(self, _now):
        if self.stopping or time.monotonic()-self.started < 120: return
        for name,monitor in self.jobs.items():
            if monitor.lock.locked(): continue
            task=self.hass.async_create_task(self.run(name,monitor))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def run(self,name,monitor):
        unexpected=False
        try:
            result=await monitor.run()
        except Exception:
            unexpected=True
            result='unavailable_or_error'
        outcome=diagnostic_outcome(result)
        previous=self.diagnostics[name]['outcome']
        now=time.monotonic()
        # Suppress repeated idle/ineligible writes; never hide a change or AI result.
        if outcome != 'skipped' or previous != outcome or now-self.diagnostic_last_publish.get(name,float('-inf')) >= 900:
            self.diagnostics[name]={'outcome':outcome, 'last_check':datetime.now(timezone.utc).isoformat()}
            if result == 'unavailable_or_error':
                detail=(None if unexpected else getattr(monitor,'last_error',None)) or {'phase':'runtime','category':'other'}
                self.diagnostics[name].update({'failure_phase':detail['phase'], 'failure_category':detail['category']})
                if detail.get('reason') in FRAME_FAILURE_REASONS:
                    self.diagnostics[name]['failure_reason']=detail['reason']
            self.diagnostic_last_publish[name]=now
            for observer in tuple(self.observers):
                try: observer(name)
                except Exception: _LOGGER.warning('Printer guard diagnostic update failed')
        if result=='unavailable_or_error':
            _LOGGER.warning('Printer guard could not assess a configured printer; no pause was sent')

    async def stop(self,_event):
        self.stopping=True
        if self.unsub: self.unsub()
        for task in self.tasks: task.cancel()
        if self.tasks: await asyncio.gather(*list(self.tasks),return_exceptions=True)

async def async_setup(hass,config):
    """Start only when explicitly configured; failure never alters existing checks."""
    if DOMAIN not in config:
        return True
    try:
        settings=parse_config(config[DOMAIN])
    except ValueError:
        _LOGGER.error('Invalid Printer Guard configuration; see the repository README')
        return False
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.core import callback
    from homeassistant.helpers.event import async_track_time_interval
    from homeassistant.helpers.discovery import async_load_platform
    from homeassistant.helpers.storage import Store
    if DOMAIN in hass.data: return True
    media=hass.config.media_dirs.get('local')
    if not media:
        _LOGGER.error('Printer guard requires a configured local media source')
        return False
    root=Path(media)
    try:
        staging=Staging(root)
        await asyncio.to_thread(staging.startup)
        latch=Latch(Store(hass,1,'p1s_guard.pause_latch'))
        await latch.load()
    except Exception as error:
        _LOGGER.error('Printer guard startup cleanup or latch loading failed (%s); monitoring disabled',safe_error_category(error))
        return False
    runtime=Runtime(hass,root,latch,settings,staging)
    hass.data[DOMAIN]=runtime
    hass.async_create_task(async_load_platform(hass,'sensor',DOMAIN,{},config))
    @callback
    def tick(now):
        runtime.tick(now)
    runtime.unsub=async_track_time_interval(hass,tick,timedelta(minutes=1))
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP,runtime.stop)
    return True
