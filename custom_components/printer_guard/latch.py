"""Durable per-printer pause-attempt reservation; no device action here."""
import asyncio
import hashlib
import json

class Latch:
    def __init__(self, store):
        self.store=store
        self.lock=asyncio.Lock()
        self.data=None

    async def load(self):
        data=await self.store.async_load()
        if data is not None and (not isinstance(data,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in data.items())):
            raise ValueError('invalid pause latch')
        self.data=data or {}

    async def reserve(self, printer, task, start):
        async with self.lock:
            if self.data is None: raise RuntimeError('latch not loaded')
            key=hashlib.sha256(json.dumps([task,start],separators=(',',':')).encode()).hexdigest()
            if self.data.get(printer)==key: return False
            updated={**self.data,printer:key}
            await self.store.async_save(updated)
            self.data=updated
            return True
