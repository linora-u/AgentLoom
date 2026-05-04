from dataclasses import dataclass
import functools


@dataclass
class TaskPayload:
    name: str
    retry: int = 0


@functools.lru_cache(maxsize=32)
def build_key(name: str) -> str:
    return name.strip().lower()


async def normalize(payload: TaskPayload) -> str:
    return payload.name.strip()


class Pipeline:
    def run(self, payload: TaskPayload) -> str:
        return build_key(payload.name)

    @staticmethod
    async def check(value: str) -> bool:
        return bool(value)


@functools.cache
async def decorated_async(value: str) -> str:
    return value
