import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";

const schema = JSON.parse(readFileSync(new URL("../../bridge-v2.schema.json", import.meta.url), "utf8"));
const validate = new Ajv2020({strict: false, strictNumbers: true, allErrors: false, validateFormats: false}).compile(schema);

export class DuplicateJsonKeyError extends SyntaxError {
  constructor(path: string) {
    super(`Duplicate JSON key at ${path}`);
    this.name = "DuplicateJsonKeyError";
  }
}

/** Check each object's original keys before JSON.parse can discard a duplicate. */
export function decodeJson(line: string): unknown {
  type Scope = {keys: Set<string> | null; path: string; key?: string; index: number};
  const keys: Scope[] = [];
  const keyPath = (path: string, key: string) => /^[A-Za-z_]\w*$/.test(key)
    ? `${path}.${key}` : `${path}[${JSON.stringify(key)}]`;
  const tokens = line.match(/"(?:[^"\\]|\\.)*"|[{}\[\]:,]|[^\s{}\[\]:,]+/g) || [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === "{" || token === "[") {
      const parent = keys.at(-1);
      const path = !parent ? "$" : parent.keys === null
        ? `${parent.path}[${parent.index}]` : keyPath(parent.path, parent.key ?? "");
      keys.push({keys: token === "{" ? new Set() : null, path, index: 0});
    }
    else if (token === "}" || token === "]") keys.pop();
    else if (token === "," && keys.at(-1)?.keys === null) keys.at(-1)!.index += 1;
    else if (token.startsWith('"') && tokens[i + 1] === ":") {
      const key = JSON.parse(token);
      const scope = keys.at(-1);
      if (!scope?.keys) throw new SyntaxError("Invalid JSON structure");
      if (scope.keys.has(key)) throw new DuplicateJsonKeyError(keyPath(scope.path, key));
      scope.keys.add(key);
      scope.key = key;
    }
  }
  return JSON.parse(line);
}

/** The same raw-key check applies to bridge protocol frames. */
export function decode(line: string): unknown {
  let result: unknown;
  try {result = decodeJson(line);} catch {throw new Error("Invalid Pi frame");}
  if (!validate(result)) throw new Error("Invalid Pi frame");
  return result;
}
