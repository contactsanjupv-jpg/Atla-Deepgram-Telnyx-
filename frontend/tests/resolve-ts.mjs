import { existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src");

function candidates(base) {
  return [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts")];
}

export async function resolve(specifier, context, next) {
  let base = null;
  if (specifier.startsWith("@/")) base = path.join(SRC, specifier.slice(2));
  else if ((specifier.startsWith("./") || specifier.startsWith("../")) && context.parentURL?.startsWith("file:")) {
    base = path.resolve(path.dirname(fileURLToPath(context.parentURL)), specifier);
  }
  if (base) {
    const hit = candidates(base).find((p) => existsSync(p) && !p.endsWith(path.sep));
    if (hit && /\.(ts|tsx)$/.test(hit)) return next(pathToFileURL(hit).href, context);
  }
  return next(specifier, context);
}