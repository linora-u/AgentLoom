import functools


async def fetch_data(x):
    return x


@functools.lru_cache()
async def cached_fetch(x):
    return x
