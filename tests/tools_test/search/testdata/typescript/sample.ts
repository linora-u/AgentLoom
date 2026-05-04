// Demo TypeScript file for grep tool integration tests.

export interface DemoPayload {
  name: string;
  id?: number;
  tags?: string[];
  metadata?: Record<string, any>;
}

export class PayloadProcessor {
  private payloads: DemoPayload[] = [];

  constructor(initialPayloads?: DemoPayload[]) {
    if (initialPayloads) {
      this.payloads = [...initialPayloads];
    }
  }

  public addPayload(payload: DemoPayload): void {
    this.payloads.push(payload);
  }

  public getPayloadNames(): string[] {
    return this.payloads.map(p => p.name);
  }
}

export function ts_target_function(payload: DemoPayload): string {
  const prefix = payload.id ? `[${payload.id}] ` : '';
  const tags = payload.tags ? ` (${payload.tags.join(', ')})` : '';
  return `${prefix}hi ${payload.name}${tags}`;
}

export const tsTargetArrow = (value: number): number => {
  if (value < 0) {
    throw new Error("Value must be positive");
  }
  return value * 2;
};

export type ComplexType = {
  data: DemoPayload;
  processor: PayloadProcessor;
  timestamp: Date;
};
