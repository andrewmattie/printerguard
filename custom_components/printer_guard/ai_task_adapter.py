"""AI Task bridge using HA services and bounded temporary media attachments."""
from __future__ import annotations
import asyncio
import json
from .media import Staging

FIELD = {
    'judgeable': {'description':'Is this frame clear enough to assess the print?', 'required':True, 'selector':{'boolean':{}}},
    'score': {'description':'Probability from 0 to 1 of a visibly failed print in this frame.', 'required':True, 'selector':{'number':{'min':0,'max':1,'step':0.01}}},
    'evidence': {'description':'Visible evidence only; empty if none. Never infer motion from one still.', 'required':True, 'selector':{'text':{}}},
}

async def _finish_file_task(task):
    """Drain an already shielded worker after cancellation, including repeats.

    Cancelling an asyncio waiter cannot stop its filesystem thread. Keep the
    result available so its private files can be cleaned before we return.
    The caller must propagate its cancellation after this filesystem work finishes.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class AITaskAdapter:
    """Caller must supply an existing AI Task, private media root and HA service bus.

    The staging root must be HA's actual local media source root. A restricted
    subfolder is created per call and removed afterwards; HA media access still
    applies. Never log instructions, images, telemetry or full responses.
    """
    def __init__(self, hass, entity_id: str, staging: Staging):
        if not entity_id.startswith('ai_task.'):
            raise ValueError('explicit AI Task entity required')
        self.hass = hass
        self.entity_id = entity_id
        self.staging = staging
        self.media_root = staging.root
        self.diagnostic_phase = 'ai_prepare'

    async def _cleanup(self, directory):
        cleanup = asyncio.create_task(asyncio.to_thread(self.staging.cleanup, directory))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as cancelled:
            try:
                await _finish_file_task(cleanup)
            finally:
                raise cancelled

    async def _ask(self, frames, telemetry, comparison):
        if len(frames) not in (1,3): raise ValueError('expected one or three frames')
        # Shield file creation so cancellation cannot race ahead of its cleanup.
        self.diagnostic_phase = 'media_prepare'
        preparation = asyncio.create_task(asyncio.to_thread(self.staging.prepare, frames))
        try:
            directory, paths = await asyncio.shield(preparation)
        except asyncio.CancelledError as cancelled:
            try:
                directory, _ = await _finish_file_task(preparation)
                self.diagnostic_phase = 'media_cleanup'
                await self._cleanup(directory)
            finally:
                raise cancelled
        try:
            self.diagnostic_phase = 'ai_request_build'
            attachments=[{'media_content_id':'media-source://media_source/local/'+str(p.relative_to(self.media_root)), 'media_content_type':'image/jpeg'} for p in paths]
            structure={f'frame_{n}_{k}': v for n in range(1,len(frames)+1) for k,v in FIELD.items()}
            question = ('Compare these three ordered still photographs of the same print. '
                        'Assess each frame independently; do not infer unobserved motion. '
                        if comparison else 'Assess this one still photograph of a 3D print. Do not infer motion. ')
            instruction=(question+'Identify only visible spaghetti, large detached material or major deformation. '
                         'If obscured, blank, frozen-looking or uncertain mark unjudgeable. '
                         'Report one score and visual evidence per frame. Telemetry is context only: '+json.dumps(telemetry, default=str))
            self.diagnostic_phase = 'ai_service'
            response=await self.hass.services.async_call('ai_task','generate_data',{
                'entity_id':self.entity_id,'task_name':'3D printer visual assessment',
                'instructions':instruction,'structure':structure,'attachments':attachments,
            },blocking=True,return_response=True)
            self.diagnostic_phase = 'ai_response'
            data=response.get('data') if isinstance(response,dict) else None
            if not isinstance(data,dict): raise ValueError('invalid AI response')
            return [{'judgeable':data.get(f'frame_{n}_judgeable'), 'score':data.get(f'frame_{n}_score'), 'evidence':data.get(f'frame_{n}_evidence')} for n in range(1,len(frames)+1)]
        finally:
            previous_phase = self.diagnostic_phase
            self.diagnostic_phase = 'media_cleanup'
            await self._cleanup(directory)
            self.diagnostic_phase = previous_phase

    async def screen(self,frame,telemetry): return (await self._ask([frame],telemetry,False))[0]
    async def compare(self,frames,telemetry): return await self._ask(frames,telemetry,True)
