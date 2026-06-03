#!/usr/bin/env node
// Regenerate src/lib/api/generated.ts from the backend's OpenAPI spec.
//
// Strategy: spawn a one-shot Python process inside the `awm` mamba env that
// imports the FastAPI app and calls `app.openapi()` directly. This bypasses
// the running awm-exposed service entirely — no auth dance, no live uvicorn
// required, no port collisions with the operator's running stack.
//
// The Python invocation prints exactly one line to stdout (the spec JSON);
// stderr is discarded. openapi-typescript consumes the JSON via its
// programmatic API and writes the TS output.

import { spawn } from 'node:child_process';
import { writeFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import openapiTS, { astToString } from 'openapi-typescript';

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(HERE, '..');
// The Python `awm` package is importable from the editable install at
// /home/tony/agentic_workspace/awm/, which is STALE relative to the current
// worktree (see memory: awm_two_source_trees). Pin spawn cwd + PYTHONPATH to
// the worktree root so `import awm` picks up THIS scope's code, not the
// editable install's.
const WORKTREE_ROOT = resolve(FRONTEND_ROOT, '..');
const OUTPUT_FILE = resolve(FRONTEND_ROOT, 'src', 'lib', 'api', 'generated.ts');

const PYTHON = process.env.AWM_PYTHON
  ?? '/home/tony/lib/miniforge3/envs/awm/bin/python';

const PYTHON_SNIPPET = `
import json, sys, os
sys.path.insert(0, os.getcwd())
sys.stderr = open('/dev/null', 'w')
from awm.exposed import app
sys.stdout.write(json.dumps(app.openapi()))
`;

const HEADER = `/* AUTO-GENERATED — do not edit by hand.
 *
 * Regenerate with: npm run gen-types
 *
 * Source: \`app.openapi()\` from awm/exposed.py, called in-process via the
 * \`awm\` mamba env. The generator does not hit a running uvicorn.
 *
 * Engine \`CONFIG_SCHEMA\` JSON Schemas escape this pipeline: FastAPI types
 * the response envelope's inner \`schema\` field as \`dict[str, Any]\`, which
 * openapi-typescript emits as \`unknown\` / \`Record<string, unknown>\`.
 * Fixtures for engine forms must hand-shape JSON Schema blobs.
 */
`;

function fetchSpec() {
  return new Promise((resolveSpec, rejectSpec) => {
    const proc = spawn(PYTHON, ['-c', PYTHON_SNIPPET], {
      stdio: ['ignore', 'pipe', 'pipe'],
      cwd: WORKTREE_ROOT,
    });
    const chunks = [];
    const errChunks = [];
    proc.stdout.on('data', (c) => chunks.push(c));
    proc.stderr.on('data', (c) => errChunks.push(c));
    proc.on('error', rejectSpec);
    proc.on('close', (code) => {
      if (code !== 0) {
        rejectSpec(new Error(
          `Python typegen exited ${code}\n` +
          `command: ${PYTHON} -c "<inline snippet>"\n` +
          `stderr: ${Buffer.concat(errChunks).toString('utf8')}`,
        ));
        return;
      }
      try {
        resolveSpec(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (e) {
        rejectSpec(new Error(`Python returned non-JSON: ${e.message}`));
      }
    });
  });
}

async function main() {
  console.error('• Loading OpenAPI spec from awm.exposed:app …');
  const spec = await fetchSpec();
  const pathCount = Object.keys(spec.paths ?? {}).length;
  const schemaCount = Object.keys(spec.components?.schemas ?? {}).length;
  console.error(`  ${pathCount} paths, ${schemaCount} schemas`);

  console.error('• Generating TypeScript …');
  const ast = await openapiTS(spec);
  const body = astToString(ast);

  await mkdir(dirname(OUTPUT_FILE), { recursive: true });
  await writeFile(OUTPUT_FILE, HEADER + '\n' + body);
  console.error(`• Wrote ${OUTPUT_FILE}`);
}

main().catch((e) => {
  console.error('gen-types failed:', e.message);
  process.exit(1);
});
