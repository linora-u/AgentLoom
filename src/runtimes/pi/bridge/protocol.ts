import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";

const schema = JSON.parse(readFileSync(new URL("../../bridge-v2.schema.json", import.meta.url), "utf8"));
const validate = new Ajv2020({strict: false, strictNumbers: true, allErrors: false, validateFormats: false}).compile(schema);

/** JSON.parse accepts repeated keys; reject them before interpreting any protocol value. */
export function decode(line: string): unknown {
  const keys: (Set<string> | null)[] = [];
  const tokens = line.match(/"(?:[^"\\]|\\.)*"|[{}\[\]:,]|[^\s{}\[\]:,]+/g) || [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === "{") keys.push(new Set());
    else if (token === "[") keys.push(null);
    else if (token === "}" || token === "]") keys.pop();
    else if (token.startsWith('"') && tokens[i + 1] === ":") {
      const key = JSON.parse(token);
      const scope = keys.at(-1);
      if (!scope || scope.has(key)) throw new Error("Invalid Pi frame");
      scope.add(key);
    }
  }
  const result = JSON.parse(line);
  if (!validate(result)) throw new Error("Invalid Pi frame");
  return result;
}
