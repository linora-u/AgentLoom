interface LocalPayload {
  id: number;
  name: string;
}

type LocalResult = {
  ok: boolean;
  payload: LocalPayload;
};

class LocalProcessor {
  process(payload: LocalPayload): LocalResult {
    return { ok: true, payload };
  }
}

enum LocalStatus {
  Idle = "idle",
  Busy = "busy",
}

function localHelper(payload: LocalPayload): LocalResult {
  return { ok: payload.id > 0, payload };
}

let mapPayload = (payload: LocalPayload): string => payload.name.toUpperCase();

const transformPayload = function(payload: LocalPayload): LocalResult {
  return { ok: true, payload };
};

export async function loadPayload(id: number): Promise<LocalPayload> {
  return { id, name: `id-${id}` };
}
