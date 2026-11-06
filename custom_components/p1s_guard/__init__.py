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

def _config_schema(config):
    """Validate YAML through HA's schema hook without importing HA in unit tests."""
    import probatio
    if DOMAIN in config:
        try:
            parse_config(config[DOMAIN])
        except ValueError:
            error=probatio.Invalid('Invalid p1s_guard configuration; see the repository README')
            error.secret=True  # HA's error formatter otherwise echoes the full YAML value.
            raise error from None
    return config

# Keep the callable hook explicit for HA and static integration validation.
CONFIG_SCHEMA = _config_schema

FAILURE_PHASES=frozenset({'context','runtime','camera_capture','frame_validation','session_check',
    'screen_ai','screen_verdict','comparison_ai','comparison_verdict','notification',
    'ai_prepare','media_prepare','ai_request_build','ai_service','ai_response','media_cleanup','ai_other'})
FAILURE_CATEGORIES=frozenset({'timeout','permission','not_found','os_error','validation','invalid_data','other'})
SKIP_REASONS={'ineligible':'printer_ineligible','session_changed':'session_changed',
    'stale_result':'assessment_expired','stale_gap':'receipt_gap','busy':'already_running',
    'already_attempted':'already_attempted','cancelled':'cancelled'}
DEGRADED_REASONS={'repeated_view':'repeated_view','screen_unjudgeable':'unjudgeable',
    'comparison_unjudgeable':'unjudgeable'}


def diagnostic_outcome(result):
    if result == 'screen_clear': return 'clear'
    if result == 'comparison_not_confirmed': return 'not_confirmed'
    if result == 'monitor_only': return 'suspicious'
    if result in DEGRADED_REASONS: return 'degraded'
    if result in SKIP_REASONS: return 'skipped'
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
        self.pending=set()
        self.last_notice={}
        self.stopping=False
        self.unsub=None
        self.diagnostics={p.prefix: {'outcome':'not_checked', 'last_check':None,
            'last_usable_assessment':None,'check_started':None,'assessment':'not_checked',
            'notification':'not_requested','freshness':'not_checked'} for p in self.printers}
        self.diagnostic_last_publish={}
        self.observers=[]
        for printer in self.printers:
            ai=AITaskAdapter(hass,settings.ai_task,self.staging)
            async def capture(p=printer): return await camera_capture(hass,p)
            def context(p=printer): return context_from_states(hass.states,p)
            async def reserve(task,start,p=printer): return await latch.reserve(p.prefix,task,start)
            async def pause(): raise RuntimeError('automatic pause is disabled')
            async def notify(message,expected,p=printer):
                current=context_from_states(hass.states,p)
                if not current.eligible or current.session != expected.session:
                    return 'suppressed_session'
                now=time.monotonic()
                previous=self.last_notice.get(p.prefix)
                if previous is not None and previous[0] == expected.job and now-previous[1] < 1800:
                    return 'suppressed_cooldown'
                await hass.services.async_call('notify',settings.notify_service.split('.',1)[1],
                    {'title':f'{p.name} print check', 'message':message},blocking=True)
                # Keep one bounded entry per printer. A different validated job
                # must receive its first alert even within the previous cooldown.
                self.last_notice[p.prefix]=(expected.job,now)
                return 'submitted'
            self.jobs[printer.prefix]=Monitor(capture=capture,screen=ai.screen,compare=ai.compare,
                context=context,reserve=reserve,pause=pause,notify=notify,clock=time.monotonic,allow_pause=False,
                ai_phase=lambda adapter=ai: adapter.diagnostic_phase,
                on_started=lambda new_job,p=printer: self.check_started(p.prefix,new_job))

    def tick(self, _now):
        if self.stopping or time.monotonic()-self.started < 120: return
        for name,monitor in self.jobs.items():
            if name in self.pending or monitor.lock.locked(): continue
            self.pending.add(name)
            task=self.hass.async_create_task(self.run(name,monitor))
            self.tasks.add(task)
            def completed(done,key=name):
                self.tasks.discard(done)
                self.pending.discard(key)
            task.add_done_callback(completed)

    def publish(self,name):
        for observer in tuple(self.observers):
            try: observer(name)
            except Exception: _LOGGER.warning('Printer guard diagnostic update failed')

    def check_started(self,name,new_job=False):
        previous=self.diagnostics[name]
        self.diagnostics[name]={
            'outcome':'checking','last_check':previous['last_check'],
            'last_usable_assessment':None if new_job else previous['last_usable_assessment'],
            'check_started':datetime.now(timezone.utc).isoformat(),
            'assessment':'not_checked','notification':'not_requested','freshness':'unknown',
        }
        self.publish(name)

    async def run(self,name,monitor):
        unexpected=cancelled=False
        try:
            result=await monitor.run()
        except asyncio.CancelledError:
            cancelled=True
            result='cancelled'
        except Exception:
            unexpected=True
            result='unavailable_or_error'
        outcome=diagnostic_outcome(result)
        previous=self.diagnostics[name]
        now=time.monotonic()
        # Suppress repeated idle/ineligible writes; never hide a change or AI result.
        if outcome != 'skipped' or previous['outcome'] != outcome or previous.get('reason') != SKIP_REASONS.get(result) or now-self.diagnostic_last_publish.get(name,float('-inf')) >= 900:
            completed=datetime.now(timezone.utc).isoformat()
            diagnostic={'outcome':outcome, 'last_check':completed,
                'last_usable_assessment':previous['last_usable_assessment'],
                'check_started':previous['check_started'],
                'assessment':'not_checked','notification':'not_requested','freshness':'not_checked'}
            freshness=getattr(monitor,'last_freshness',None)
            if freshness in ('not_checked','unknown','suspected_stale'):
                diagnostic['freshness']=freshness
            assessment=getattr(monitor,'last_assessment',None)
            if assessment in ('not_checked','clear','unjudgeable','not_confirmed','suspicious','pending_comparison'):
                diagnostic['assessment']=assessment
            notification=getattr(monitor,'last_notification',None)
            if notification in ('not_requested','submitted','suppressed_cooldown','suppressed_session','unknown','timed_out','failed','cancelled'):
                diagnostic['notification']=notification
            if (not unexpected and not cancelled and getattr(monitor,'assessment_usable',False) is True
                    and freshness == 'unknown' and assessment in ('clear','not_confirmed','suspicious')):
                diagnostic['last_usable_assessment']=completed
            if result in SKIP_REASONS:
                diagnostic['reason']=SKIP_REASONS[result]
            elif result in DEGRADED_REASONS:
                diagnostic['reason']=DEGRADED_REASONS[result]
            if result == 'unavailable_or_error':
                detail=(None if unexpected else getattr(monitor,'last_error',None))
                detail=detail if isinstance(detail,dict) else {}
                diagnostic['failure_phase']=detail.get('phase') if isinstance(detail.get('phase'),str) and detail['phase'] in FAILURE_PHASES else 'runtime'
                diagnostic['failure_category']=detail.get('category') if isinstance(detail.get('category'),str) and detail['category'] in FAILURE_CATEGORIES else 'other'
                if isinstance(detail.get('reason'),str) and detail['reason'] in FRAME_FAILURE_REASONS:
                    diagnostic['failure_reason']=detail['reason']
            self.diagnostics[name]=diagnostic
            self.diagnostic_last_publish[name]=now
            self.publish(name)
        if result=='unavailable_or_error':
            _LOGGER.warning('Printer guard check failed; see diagnostic labels')
        if cancelled:
            raise asyncio.CancelledError

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
