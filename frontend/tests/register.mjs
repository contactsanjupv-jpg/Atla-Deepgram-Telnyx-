// Lets node's test runner import the app's extensionless TypeScript modules ("./billing" -> billing.ts, "@/lib/x" -> src/lib/x.ts).
import { register } from "node:module";
register("./resolve-ts.mjs", import.meta.url);