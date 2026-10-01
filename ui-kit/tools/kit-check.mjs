#!/usr/bin/env node
/**
 * kit:check -- the drift check for a synced copy of shared-infra/ui-kit, runnable with nothing but Node.
 *
 * sync.py writes kit.lock.json with a sha256 per synced file. This verifies those files against it, so a
 * hand edit to src/kit/ (which the next sync would silently overwrite) fails `npm run lint`, in CI, in
 * Docker and on a machine without shared-infra checked out. Line endings are normalised before hashing,
 * so a CRLF checkout (git core.autocrlf on Windows) is not drift.
 *
 * Usage (from the app's frontend root): node scripts/kit-check.mjs
 */
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const root = process.cwd();
const lockPath = join(root, 'kit.lock.json');
if (!existsSync(lockPath)) {
  console.error('kit:check: no kit.lock.json here. Run: python <shared-infra>/ui-kit/sync.py .');
  process.exit(1);
}
const lock = JSON.parse(readFileSync(lockPath, 'utf8'));
const hash = (text) => createHash('sha256').update(text.replace(/\r\n/g, '\n'), 'utf8').digest('hex');

function walk(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : [relative(root, full).split(sep).join('/')];
  });
}

const problems = [];
for (const [path, want] of Object.entries(lock.files)) {
  const full = join(root, path);
  if (!existsSync(full)) problems.push(`missing: ${path}`);
  else if (hash(readFileSync(full, 'utf8')) !== want) problems.push(`edited:  ${path}`);
}
for (const path of walk(join(root, lock.kit_dir))) {
  if (!(path in lock.files)) problems.push(`extra:   ${path}`);
}

if (problems.length) {
  console.error(`kit:check: ${problems.length} file(s) differ from ${lock.source} (kit.lock.json):`);
  for (const p of problems) console.error(`  ${p}`);
  console.error(`Edit the kit in ${lock.source}, then run: python <shared-infra>/ui-kit/sync.py .`);
  process.exit(1);
}
console.log(`kit:check: ${Object.keys(lock.files).length} files match ${lock.source}.`);
